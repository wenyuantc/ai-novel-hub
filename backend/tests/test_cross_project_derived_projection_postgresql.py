"""跨书派生引用在隔离 PostgreSQL 与跨连接变化下不得外泄。"""

import httpx
import pytest

from app.models import Chapter, Character, Organization, OrganizationMember, Outline, Relationship, RelationshipType
from app.services.open_api import entities
from tests.helpers import create_project, register
from tests.test_open_api import new_key

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def _world(owner_app, owner, victim, viewer):
    await register(owner, "derived-pg-owner")
    await register(victim, "derived-pg-victim")
    await register(viewer, "derived-pg-viewer")
    book = await create_project(owner, title="本人作品")
    other = await create_project(victim, title="不可见作品")
    base = f"/api/v1/projects/{book['id']}"
    invite = await owner.post(f"{base}/collaborators", json={"username": "derived-pg-viewer", "role": "viewer"})
    assert invite.status_code == 201, invite.text
    accepted = await viewer.post(
        f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
        json={"expected_revision": 1},
    )
    assert accepted.status_code == 200, accepted.text
    async with owner_app.state.sessionmaker() as db:
        people = [Character(project_id=book["id"], name=f"本人角色{index}") for index in range(3)]
        foreign = Character(project_id=other["id"], name="PRIVATE_CHARACTER")
        foreign_org = Organization(project_id=other["id"], name="PRIVATE_ORGANIZATION")
        foreign_outline = Outline(project_id=other["id"], title="不可见大纲", volume_no=1,
                                  volume_title="PRIVATE_VOLUME", chapter_no=1)
        global_type = RelationshipType(project_id=None, role_a="公开甲", role_b="公开乙", category="social")
        foreign_type = RelationshipType(project_id=other["id"], role_a="PRIVATE_ROLE", role_b="外书", category="social")
        db.add_all([*people, foreign, foreign_org, foreign_outline, global_type, foreign_type])
        await db.flush()
        org = Organization(project_id=book["id"], name="本人组织", parent_id=foreign_org.id)
        chapter = Chapter(project_id=book["id"], chapter_no=1, title="本人章节", content="正文", status="done",
                          outline_id=foreign_outline.id)
        db.add_all([org, chapter])
        await db.flush()
        invalid = OrganizationMember(organization_id=org.id, character_id=foreign.id, status="active")
        member = OrganizationMember(organization_id=org.id, character_id=people[0].id, status="active")
        bad = Relationship(project_id=book["id"], a_id=people[0].id, b_id=foreign.id,
                           pair_low=min(people[0].id, foreign.id), pair_high=max(people[0].id, foreign.id),
                           type_id=foreign_type.id, intimacy=0, status="normal")
        good = Relationship(project_id=book["id"], a_id=people[1].id, b_id=people[2].id,
                            pair_low=min(people[1].id, people[2].id), pair_high=max(people[1].id, people[2].id),
                            type_id=global_type.id, intimacy=0, status="normal")
        db.add_all([invalid, member, bad, good])
        await db.commit()
        return book, {"org": org.id, "member": member.id, "bad_member": invalid.id, "bad": bad.id, "good": good.id,
                      "foreign": foreign.id, "foreign_type": foreign_type.id, "global_type": global_type.id}


async def test_postgres_open_and_collaborator_project_cross_references_fail_closed(postgres_collaboration_apps):
    owner_app, other_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://testserver") as victim,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://testserver") as viewer,
    ):
        book, ids = await _world(owner_app, owner, victim, viewer)
        headers = await new_key(owner, [book["id"]])
        open_base = f"/api/open/v1/novels/{book['id']}"
        chapters = await owner.get(f"{open_base}/chapters", headers=headers)
        assert chapters.status_code == 200 and chapters.json()["items"][0]["volume_title"] is None
        members = await owner.get(f"{open_base}/organizations/{ids['org']}/members", headers=headers,
                                  params={"page_size": 1})
        assert members.status_code == 200 and members.json()["total"] == 1
        assert [row["id"] for row in members.json()["items"]] == [ids["member"]]
        relations = await owner.get(f"{open_base}/relationships", headers=headers, params={"page_size": 1})
        assert relations.status_code == 200 and relations.json()["total"] == 1
        assert relations.json()["items"][0]["id"] == ids["good"]
        assert relations.json()["items"][0]["type_id"] == ids["global_type"]
        for rel_id in (ids["bad"], 2147483647):
            assert (await owner.get(f"{open_base}/relationships/{rel_id}", headers=headers)).status_code == 404
        for suffix in ("/relationships", "/relationship-graph", f"/organizations/{ids['org']}/members"):
            response = await viewer.get(f"/api/v1/projects/{book['id']}" + suffix)
            assert response.status_code == 200, response.text
            assert all(secret not in response.text
                       for secret in ("PRIVATE_CHARACTER", "PRIVATE_ROLE", "PRIVATE_VOLUME"))


@pytest.mark.parametrize("target", ["member", "relationship"])
async def test_postgres_changed_link_during_pagination_fails_closed(postgres_collaboration_apps, monkeypatch, target):
    owner_app, other_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://testserver") as victim,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://testserver") as viewer,
    ):
        book, ids = await _world(owner_app, owner, victim, viewer)
        headers = await new_key(owner, [book["id"]])
        original = entities.page_models

        async def change_after_page(db, statement, page, page_size):
            records, total = await original(db, statement, page, page_size)
            async with other_app.state.sessionmaker() as writer:
                if target == "member":
                    # 先移走原无效成员，避免唯一约束掩盖要测试的跨书替换。
                    bad = await writer.get(OrganizationMember, ids["bad_member"])
                    await writer.delete(bad)
                    (await writer.get(OrganizationMember, ids["member"])).character_id = ids["foreign"]
                else:
                    rel = await writer.get(Relationship, ids["good"])
                    rel.type_id = ids["foreign_type"]
                await writer.commit()
            return records, total

        monkeypatch.setattr(entities, "page_models", change_after_page)
        base = f"/api/open/v1/novels/{book['id']}"
        suffix = f"/organizations/{ids['org']}/members" if target == "member" else "/relationships"
        response = await owner.get(base + suffix, headers=headers)
        assert response.status_code == 404, response.text
        assert all(secret not in response.text for secret in ("PRIVATE_CHARACTER", "PRIVATE_ROLE"))
        assert "total" not in response.json()
