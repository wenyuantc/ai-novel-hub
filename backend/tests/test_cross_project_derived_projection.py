"""无权作品关联即使已在历史数据库中损坏，也不能通过派生响应外泄。"""

import pytest

from app.models import Chapter, Character, Organization, OrganizationMember, Outline, Relationship, RelationshipType
from app.services import relationship_service
from tests.helpers import create_project, register
from tests.test_open_api import new_key


@pytest.fixture
async def cross_book(app, client, make_client, system_admin):
    owner = await register(client, "derived-owner")
    victim_client, viewer, editor = make_client(), make_client(), make_client()
    await register(victim_client, "derived-victim")
    await register(viewer, "derived-viewer")
    await register(editor, "derived-editor")
    book = await create_project(client, title="本人作品")
    other = await create_project(victim_client, title="不可见作品")
    base = f"/api/v1/projects/{book['id']}"
    for actor, username, role in (
        (viewer, "derived-viewer", "viewer"),
        (editor, "derived-editor", "editor"),
        (system_admin, "settings_admin", "viewer"),
    ):
        invite = await client.post(f"{base}/collaborators", json={"username": username, "role": role})
        assert invite.status_code == 201, invite.text
        accepted = await actor.post(
            f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
            json={"expected_revision": 1},
        )
        assert accepted.status_code == 200, accepted.text
    async with app.state.sessionmaker() as db:
        own = [Character(project_id=book["id"], name=f"本人角色{index}") for index in range(1, 5)]
        foreign = Character(project_id=other["id"], name="PRIVATE_CHARACTER")
        foreign_parent = Organization(project_id=other["id"], name="PRIVATE_ORGANIZATION")
        foreign_outline = Outline(project_id=other["id"], volume_no=7, volume_title="PRIVATE_VOLUME",
                                  chapter_no=1, title="外书大纲")
        own_outline = Outline(project_id=book["id"], volume_no=2, volume_title="本书卷名",
                              chapter_no=2, title="本书大纲")
        foreign_type = RelationshipType(project_id=other["id"], role_a="PRIVATE_ROLE", role_b="外书关系",
                                        category="social")
        global_type = RelationshipType(project_id=None, role_a="公开甲", role_b="公开乙", category="social")
        db.add_all([*own, foreign, foreign_parent, foreign_outline, own_outline, foreign_type, global_type])
        await db.flush()
        org = Organization(project_id=book["id"], name="本人组织", parent_id=foreign_parent.id)
        bad_chapter = Chapter(project_id=book["id"], chapter_no=1, title="跨书大纲章", content="正文",
                              status="done", outline_id=foreign_outline.id)
        good_chapter = Chapter(project_id=book["id"], chapter_no=2, title="本书大纲章", content="正文",
                               status="done", outline_id=own_outline.id)
        db.add_all([org, bad_chapter, good_chapter])
        await db.flush()
        bad_member = OrganizationMember(organization_id=org.id, character_id=foreign.id,
                                        position="跨书", status="active")
        good_member = OrganizationMember(organization_id=org.id, character_id=own[0].id,
                                         position="本书", status="active")

        def relation(a, b, kind):
            return Relationship(project_id=book["id"], a_id=a.id, b_id=b.id, pair_low=min(a.id, b.id),
                                pair_high=max(a.id, b.id), type_id=kind.id, intimacy=0, status="normal")

        bad_character = relation(own[0], foreign, global_type)
        bad_type = relation(own[0], own[1], foreign_type)
        good = relation(own[2], own[3], global_type)
        db.add_all([bad_member, good_member, bad_character, bad_type, good])
        await db.commit()
        ids = {
            "org": org.id, "foreign_parent": foreign_parent.id, "foreign_character": foreign.id,
            "foreign_outline": foreign_outline.id, "bad_chapter": bad_chapter.id, "good_chapter": good_chapter.id,
            "bad_member": bad_member.id, "good_member": good_member.id,
            "bad_relations": [bad_character.id, bad_type.id], "good_relation": good.id,
            "global_type": global_type.id, "own_characters": [row.id for row in own],
        }
    headers = await new_key(client, [book["id"]])
    return owner, book, other, ids, headers, (client, viewer, editor, system_admin), victim_client


async def test_cross_project_references_are_not_projected_in_manual_and_open_reads(app, client, cross_book):
    _, book, _, ids, headers, actors, _ = cross_book
    base = f"/api/v1/projects/{book['id']}"
    open_base = f"/api/open/v1/novels/{book['id']}"
    for actor in actors:
        chapters = await actor.get(f"{base}/chapters")
        assert chapters.status_code == 200, chapters.text
        by_id = {row["id"]: row for row in chapters.json()}
        assert by_id[ids["bad_chapter"]]["outline_id"] is None

        organization = await actor.get(f"{base}/organizations/{ids['org']}")
        assert organization.status_code == 200, organization.text
        assert organization.json()["parent_id"] is None and organization.json()["member_count"] == 1
        members = await actor.get(f"{base}/organizations/{ids['org']}/members")
        assert members.status_code == 200, members.text
        assert [row["id"] for row in members.json()] == [ids["good_member"]]
        relations = await actor.get(f"{base}/relationships")
        assert relations.status_code == 200, relations.text
        assert [row["id"] for row in relations.json()] == [ids["good_relation"]]
        assert relations.json()[0]["type_id"] == ids["global_type"]
        graph = await actor.get(f"{base}/relationship-graph")
        assert graph.status_code == 200, graph.text
        assert [row["id"] for row in graph.json()["edges"]] == [ids["good_relation"]]
        assert graph.json()["organizations"][0]["parent_id"] is None
        for response in (chapters, organization, members, relations, graph):
            assert all(secret not in response.text
                       for secret in ("PRIVATE_VOLUME", "PRIVATE_CHARACTER", "PRIVATE_ROLE"))

    chapters = await client.get(f"{open_base}/chapters", headers=headers)
    assert chapters.status_code == 200, chapters.text
    by_id = {row["id"]: row for row in chapters.json()["items"]}
    assert by_id[ids["bad_chapter"]]["volume_no"] is None and by_id[ids["bad_chapter"]]["volume_title"] is None
    assert by_id[ids["good_chapter"]]["volume_title"] == "本书卷名"
    detail = await client.get(f"{open_base}/chapters/{ids['bad_chapter']}", headers=headers)
    assert detail.status_code == 200 and detail.json()["volume_title"] is None
    organization = await client.get(f"{open_base}/organizations/{ids['org']}", headers=headers)
    assert organization.status_code == 200, organization.text
    assert organization.json()["parent_id"] is None and organization.json()["member_count"] == 1
    members = await client.get(f"{open_base}/organizations/{ids['org']}/members", headers=headers,
                               params={"page_size": 1})
    assert members.status_code == 200 and members.json()["total"] == 1, members.text
    assert [row["id"] for row in members.json()["items"]] == [ids["good_member"]]
    relations = await client.get(f"{open_base}/relationships", headers=headers, params={"page_size": 1})
    assert relations.status_code == 200 and relations.json()["total"] == 1, relations.text
    assert [row["id"] for row in relations.json()["items"]] == [ids["good_relation"]]
    valid = await client.get(f"{open_base}/relationships/{ids['good_relation']}", headers=headers)
    assert valid.status_code == 200 and valid.json()["type_id"] == ids["global_type"]
    missing = await client.get(f"{open_base}/relationships/2147483647", headers=headers)
    for rel_id in ids["bad_relations"]:
        hidden = await client.get(f"{open_base}/relationships/{rel_id}", headers=headers)
        assert hidden.status_code == missing.status_code == 404 and hidden.json() == missing.json()
    for response in (chapters, detail, organization, members, relations, valid):
        assert all(secret not in response.text for secret in ("PRIVATE_VOLUME", "PRIVATE_CHARACTER", "PRIVATE_ROLE"))
    async with app.state.sessionmaker() as db:
        # AI 上下文同样不能按人物 ID 把别书类型带入模型。
        contextual = await relationship_service.relations_among(db, ids["own_characters"])
        assert [row.id for row, _ in contextual] == [ids["good_relation"]]


async def test_cross_project_ids_cannot_be_used_to_mutate_or_probe(client, cross_book):
    _, book, _, ids, _, _, _ = cross_book
    base = f"/api/v1/projects/{book['id']}"
    bad_member = await client.patch(f"{base}/organizations/{ids['org']}/members/{ids['bad_member']}", json={})
    absent_member = await client.patch(f"{base}/organizations/{ids['org']}/members/2147483647", json={})
    assert bad_member.status_code == absent_member.status_code == 404 and bad_member.json() == absent_member.json()
    for rel_id in ids["bad_relations"]:
        hidden = await client.get(f"{base}/relationships/{rel_id}/history")
        absent = await client.get(f"{base}/relationships/2147483647/history")
        assert hidden.status_code == absent.status_code == 404 and hidden.json() == absent.json()
    hidden_pair = await client.get(f"{base}/relationship-history", params={
        "character_id": ids["own_characters"][0], "related_character_id": ids["own_characters"][1],
    })
    absent_pair = await client.get(f"{base}/relationship-history", params={
        "character_id": ids["own_characters"][1], "related_character_id": ids["own_characters"][2],
    })
    assert hidden_pair.status_code == absent_pair.status_code == 200 and hidden_pair.json() == absent_pair.json() == []
    bad_create = await client.post(f"{base}/organizations/{ids['org']}/members", json={
        "character_id": ids["foreign_character"], "position": "跨书伪造",
    })
    assert bad_create.status_code == 400 and "PRIVATE" not in bad_create.text
    bad_relation = await client.post(f"{base}/relationships", json={
        "a_id": ids["own_characters"][1], "b_id": ids["foreign_character"], "type_id": ids["global_type"],
    })
    assert bad_relation.status_code == 400 and "PRIVATE" not in bad_relation.text


async def test_missing_historical_references_fail_closed_without_breaking_valid_rows(app, client, cross_book):
    _, book, _, ids, headers, _, _ = cross_book
    # 模拟开启 FK 之前的历史损坏；单个专用连接重启约束后再运行真实 HTTP。
    async with app.state.engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await conn.exec_driver_sql("UPDATE chapters SET outline_id=2147483647 WHERE id=?", (ids["bad_chapter"],))
        await conn.exec_driver_sql("UPDATE organizations SET parent_id=2147483647 WHERE id=?", (ids["org"],))
        await conn.exec_driver_sql("UPDATE organization_members SET character_id=2147483647 WHERE id=?",
                                   (ids["bad_member"],))
        await conn.exec_driver_sql("UPDATE relationships SET type_id=2147483647 WHERE id=?", (ids["bad_relations"][0],))
        await conn.commit()
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    open_base = f"/api/open/v1/novels/{book['id']}"
    chapters = await client.get(f"{open_base}/chapters", headers=headers)
    assert chapters.status_code == 200, chapters.text
    rows = {row["id"]: row for row in chapters.json()["items"]}
    assert rows[ids["bad_chapter"]]["volume_title"] is None
    assert rows[ids["good_chapter"]]["volume_title"] == "本书卷名"
    org = await client.get(f"{open_base}/organizations/{ids['org']}", headers=headers)
    assert org.status_code == 200 and org.json()["parent_id"] is None and org.json()["member_count"] == 1
    members = await client.get(f"{open_base}/organizations/{ids['org']}/members", headers=headers)
    assert members.status_code == 200 and members.json()["total"] == 1
    relations = await client.get(f"{open_base}/relationships", headers=headers)
    assert relations.status_code == 200 and relations.json()["total"] == 1
    missing = await client.get(f"{open_base}/relationships/{ids['bad_relations'][0]}", headers=headers)
    absent = await client.get(f"{open_base}/relationships/2147483647", headers=headers)
    assert missing.status_code == absent.status_code == 404 and missing.json() == absent.json()
