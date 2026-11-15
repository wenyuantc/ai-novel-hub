"""跨作品人物 ID 不得暴露其他作品中的人物关系。"""

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from app.core.errors import BadRequestError
from app.models import Project, Relationship, RelationshipType, SettingTimeline
from app.services import relationship_service
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role
from tests.test_collaboration_story_access import _invite


@pytest.mark.parametrize("role", ["owner", "editor", "viewer", "admin", "admin_viewer", "outsider"])
async def test_cross_project_pair_has_same_error_for_existing_and_absent_relation(
    client, make_client, system_admin, role,
):
    await register(client, "relation-scope-owner")
    guest = make_client()
    guest_user = await register(guest, "relation-scope-guest")
    project = await create_project(client)
    foreign = await create_project(client, title="另一部作品")
    base = f"/api/v1/projects/{project['id']}"
    foreign_base = f"/api/v1/projects/{foreign['id']}"

    local = []
    for name in ("本书甲", "本书乙"):
        response = await client.post(f"{base}/characters", json={"name": name})
        assert response.status_code == 201, response.text
        local.append(response.json()["id"])
    foreign_ids = []
    for name in ("别书甲", "别书乙", "别书丙"):
        response = await client.post(f"{foreign_base}/characters", json={"name": name})
        assert response.status_code == 201, response.text
        foreign_ids.append(response.json()["id"])

    relation = {"role_a": "师父", "role_b": "徒弟"}
    for url, ids in ((base, local), (foreign_base, foreign_ids[:2])):
        created = await client.post(f"{url}/relationships", json={**relation, "a_id": ids[0], "b_id": ids[1]})
        assert created.status_code == 201, created.text

    if role in {"editor", "viewer", "admin_viewer"}:
        await _invite(client, guest, project["id"], "relation-scope-guest", "editor" if role == "editor" else "viewer")
    if role in {"admin", "admin_viewer"}:
        admin_role = await create_role(system_admin, f"关系编辑-{role}", ["projects.read", "projects.edit"])
        await assign_roles(system_admin, guest_user["id"], [admin_role["id"]])
    actor = client if role == "owner" else guest
    payload = {"role_a": "隐私验证师", "role_b": "隐私验证徒", "a_id": foreign_ids[0]}
    existing = await actor.post(f"{base}/relationships", json={**payload, "b_id": foreign_ids[1]})
    absent = await actor.post(f"{base}/relationships", json={**payload, "b_id": foreign_ids[2]})
    duplicate = await actor.post(f"{base}/relationships", json={**relation, "a_id": local[0], "b_id": local[1]})
    if role == "viewer":
        assert existing.status_code == absent.status_code == duplicate.status_code == 403
    elif role == "outsider":
        assert existing.status_code == absent.status_code == duplicate.status_code == 404
    else:
        assert existing.status_code == absent.status_code == 400
        assert existing.json() == absent.json() == {"detail": "角色不存在或不属于该项目"}
        assert duplicate.status_code == 409 and duplicate.json()["detail"] == "这两个角色之间已有关系"
    types = await client.get(f"{base}/relationship-types")
    assert all(item["role_a"] != "隐私验证师" for item in types.json())


async def test_cross_project_pair_rejection_does_not_flush_caller_state(app, client):
    await register(client, "relation-scope-transaction-owner")
    project = await create_project(client, title="原项目标题")
    foreign = await create_project(client, title="别的作品")
    foreign_base = f"/api/v1/projects/{foreign['id']}"
    ids = []
    for name in ("甲", "乙"):
        result = await client.post(f"{foreign_base}/characters", json={"name": name})
        assert result.status_code == 201, result.text
        ids.append(result.json()["id"])
    result = await client.post(f"{foreign_base}/relationships", json={
        "a_id": ids[0], "b_id": ids[1], "role_a": "父", "role_b": "子",
    })
    assert result.status_code == 201, result.text

    async with app.state.sessionmaker() as db:
        record = await db.get(Project, project["id"])
        record.title = "外层待提交编辑"
        with pytest.raises(BadRequestError, match="角色不存在或不属于该项目"):
            await relationship_service.create_relation(
                db, project["id"], a_id=ids[0], b_id=ids[1], role_a="新关系", role_b="新关系", source="manual",
            )
        assert sa_inspect(record).attrs.title.history.has_changes()
        with db.no_autoflush:
            assert await db.scalar(select(Project.title).where(Project.id == project["id"])) == "原项目标题"


@pytest.mark.parametrize("is_present", [False, True])
async def test_corrupt_foreign_pair_is_never_reused_or_disclosed(app, client, make_client, is_present):
    await register(client, "relation-scope-corrupt-owner")
    editor = make_client()
    await register(editor, "relation-scope-corrupt-editor")
    project, foreign = await create_project(client), await create_project(client, title="另一部作品")
    base = f"/api/v1/projects/{project['id']}"
    characters = []
    for name in ("本书甲", "本书乙"):
        response = await client.post(f"{base}/characters", json={"name": name})
        assert response.status_code == 201, response.text
        characters.append(response.json()["id"])
    await _invite(client, editor, project["id"], "relation-scope-corrupt-editor", "editor")
    async with app.state.sessionmaker() as db:
        relation_type = RelationshipType(
            project_id=foreign["id"], role_a="隐藏类型甲", role_b="隐藏类型乙", category="other", sort=1,
        )
        db.add(relation_type)
        await db.flush()
        relation = Relationship(
            project_id=foreign["id"], a_id=characters[0], b_id=characters[1],
            pair_low=min(characters), pair_high=max(characters), type_id=relation_type.id, is_present=is_present,
        )
        db.add(relation)
        await db.commit()
        relation_id = relation.id
    result = await editor.post(f"{base}/relationships", json={
        "a_id": characters[0], "b_id": characters[1], "role_a": "朋友", "role_b": "朋友",
    })
    assert result.status_code == 409 and result.json() == {"detail": "这两个角色之间已有关系"}
    async with app.state.sessionmaker() as db:
        relation = await db.get(Relationship, relation_id)
        assert relation.project_id == foreign["id"] and relation.is_present is is_present
        assert await db.scalar(select(SettingTimeline.id).where(
            SettingTimeline.project_id == foreign["id"], SettingTimeline.subject_kind == "relationship",
            SettingTimeline.character_id == min(characters), SettingTimeline.related_character_id == max(characters),
        )) is None
