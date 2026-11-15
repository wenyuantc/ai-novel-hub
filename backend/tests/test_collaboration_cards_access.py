"""卡片协作按真实角色授权，导出不得跨作品读取异常引用。"""

import pytest
from sqlalchemy import select

from app.core.errors import BadRequestError, ForbiddenError
from app.models import (
    Career,
    CareerStage,
    Character,
    CharacterCareer,
    Project,
    Relationship,
    RelationshipType,
    SettingTimeline,
)
from app.schemas.card import CardImportRequest
from app.schemas.mentions import EntityRef
from app.services import card_service
from app.services.mentions.context import MentionCandidate
from app.services.mentions.matcher import TrackingRule
from app.services.setting_ai_context import _detected, build_scope
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role


async def _invite(owner, guest, project_id, username, role):
    invitation = await owner.post(f"/api/v1/projects/{project_id}/collaborators", json={
        "username": username, "role": role,
    })
    assert invitation.status_code == 201, invitation.text
    item = invitation.json()
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{item['invitation_id']}/accept",
        json={"expected_revision": item["revision"]},
    )
    assert accepted.status_code == 200, accepted.text


async def _post(client, url, body, status):
    response = await client.post(url, json=body)
    assert response.status_code == status, response.text
    return response


async def test_card_http_access_roles_and_admin_union(client, make_client, system_admin, fake_llm):
    await register(client, "card-owner")
    viewer, editor, outsider, admin, admin_viewer = [make_client() for _ in range(5)]
    await register(viewer, "card-viewer")
    editor_user = await register(editor, "card-editor")
    await register(outsider, "card-outsider")
    admin_user = await register(admin, "card-admin")
    admin_viewer_user = await register(admin_viewer, "card-admin-viewer")
    role = await create_role(system_admin, "卡片管理读写", ["projects.read", "projects.edit"])
    await assign_roles(system_admin, admin_user["id"], [role["id"]])
    await assign_roles(system_admin, admin_viewer_user["id"], [role["id"]])
    project = await create_project(client)
    foreign = await create_project(client, title="另一作品")
    base = f"/api/v1/projects/{project['id']}"
    char = await _post(client, f"{base}/characters", {"name": "已有角色"}, 201)
    foreign_char = await _post(client, f"/api/v1/projects/{foreign['id']}/characters", {"name": "他书"}, 201)
    for user, name, role in ((viewer, "card-viewer", "viewer"), (editor, "card-editor", "editor"),
                             (admin_viewer, "card-admin-viewer", "viewer")):
        await _invite(client, user, project["id"], name, role)

    export_body = {"character_ids": [char.json()["id"]]}
    card = (await _post(client, f"{base}/cards/export", export_body, 200)).json()
    assert card["characters"][0]["name"] == "已有角色"
    for member in (viewer, editor, admin_viewer):
        exported = await _post(member, f"{base}/cards/export", export_body, 200)
        assert exported.json()["characters"] == card["characters"]
        await _post(member, f"{base}/cards/export", {"character_ids": [foreign_char.json()["id"]]}, 400)
        await _post(member, f"{base}/cards/import/preview", {"data": card}, 200)
    for denied in (outsider, admin):
        await _post(denied, f"{base}/cards/export", export_body, 404)
        await _post(denied, f"{base}/cards/import/preview", {"data": card}, 404)

    legacy = {"format": "ainovel.cards", "version": 1, "characters": [{"name": "导入角色"}]}
    await _post(viewer, f"{base}/cards/import", {"data": legacy}, 403)
    await _post(outsider, f"{base}/cards/import", {"data": legacy}, 404)
    await _post(admin, f"{base}/cards/import", {"data": legacy}, 404)
    await _post(editor, f"{base}/cards/import", {"data": legacy}, 200)
    admin_card = {"format": "ainovel.cards", "version": 1, "characters": [{"name": "管理员协作写入"}]}
    await _post(admin_viewer, f"{base}/cards/import", {"data": admin_card}, 200)
    pending = await _post(client, f"{base}/collaborators", {"username": "card-outsider", "role": "viewer"}, 201)
    await _post(outsider, f"{base}/cards/export", export_body, 404)
    declined = await _post(outsider,
        f"/api/v1/collaboration/invitations/project/{pending.json()['invitation_id']}/decline",
        {"expected_revision": pending.json()["revision"]}, 200)
    assert declined.json()["status"] == "declined"
    await _post(outsider, f"{base}/cards/import/preview", {"data": card}, 404)
    revoked = await client.delete(f"{base}/collaborators/{editor_user['id']}", params={"expected_revision": 2})
    assert revoked.status_code == 204, revoked.text
    await _post(editor, f"{base}/cards/export", export_body, 404)
    await _post(editor, f"{base}/cards/import", {"data": legacy}, 404)
    assert not fake_llm.calls and not fake_llm.embed_calls and not fake_llm.image_calls


@pytest.mark.parametrize("corruption", ["career", "stage", "relationship", "relationship_type"])
async def test_card_export_rejects_corrupt_cross_project_associations(app, client, make_client, corruption):
    await register(client, "card-cross-owner")
    viewer = make_client()
    await register(viewer, "card-cross-viewer")
    project = await create_project(client)
    foreign = await create_project(client, title="其他作品")
    base = f"/api/v1/projects/{project['id']}"
    first = (await _post(client, f"{base}/characters", {"name": "甲"}, 201)).json()
    second = (await _post(client, f"{base}/characters", {"name": "乙"}, 201)).json()
    await _invite(client, viewer, project["id"], "card-cross-viewer", "viewer")
    async with app.state.sessionmaker() as db:
        local = Career(project_id=project["id"], name="本书职业")
        other = Career(project_id=foreign["id"], name="不可泄露职业", description="不可泄露正文")
        db.add_all([local, other])
        await db.flush()
        local_stage = CareerStage(career_id=local.id, position=1, name="本书阶段")
        other_stage = CareerStage(career_id=other.id, position=1, name="不可泄露阶段")
        db.add_all([local_stage, other_stage])
        await db.flush()
        if corruption in {"career", "stage"}:
            db.add(CharacterCareer(
                character_id=first["id"], career_id=other.id if corruption == "career" else local.id,
                stage_id=other_stage.id, role="main",
            ))
        else:
            rel_type = RelationshipType(project_id=foreign["id"] if corruption == "relationship_type" else None,
                                        role_a="不可泄露关系", role_b="关系")
            db.add(rel_type)
            await db.flush()
            db.add(Relationship(project_id=foreign["id"] if corruption == "relationship" else project["id"],
                                a_id=first["id"], b_id=second["id"], pair_low=first["id"],
                                pair_high=second["id"], type_id=rel_type.id, description="不可泄露正文"))
        await db.commit()
    response = await _post(viewer, f"{base}/cards/export", {"character_ids": [first["id"], second["id"]]}, 409)
    assert "不可泄露" not in response.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Career.name).where(Career.project_id == foreign["id"])) == "不可泄露职业"


async def test_card_conflict_warnings_hide_series_from_book_only_collaborators(client, make_client):
    await register(client, "card-series-owner")
    viewer, editor = make_client(), make_client()
    await register(viewer, "card-series-viewer")
    await register(editor, "card-series-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    series = (await _post(client, "/api/v1/series", {"name": "隐藏系列"}, 201)).json()
    series_url = f"/api/v1/series/{series['id']}"
    series = (await _post(client, f"{series_url}/books", {
        "project_id": project["id"], "expected_revision": series["revision"],
    }, 200)).json()
    entry = (await _post(client, f"{series_url}/entries", {
        "kind": "character", "data": {"name": "同名角色"}, "expected_revision": series["revision"],
    }, 201)).json()
    await _post(client, f"{series_url}/entries/{entry['item']['id']}/uses", {
        "project_id": project["id"], "expected_revision": entry["revision"],
    }, 200)
    await _invite(client, viewer, project["id"], "card-series-viewer", "viewer")
    await _invite(client, editor, project["id"], "card-series-editor", "editor")
    card = {"format": "ainovel.cards", "version": 1, "characters": [{"name": "同名角色"}]}
    owner_preview = await _post(client, f"{base}/cards/import/preview", {"data": card}, 200)
    assert "系列" in owner_preview.text
    viewer_preview = await _post(viewer, f"{base}/cards/import/preview", {"data": card}, 200)
    assert "系列" not in viewer_preview.text
    result = await _post(editor, f"{base}/cards/import", {"data": card}, 200)
    assert "系列" not in result.text
    assert result.json()["characters"][0]["id"] is None


@pytest.mark.parametrize("version", [1, 6])
async def test_card_candidate_competition_invalidates_visibility_and_history(app, client, make_client, version):
    await register(client, "card-collision-owner")
    editor = make_client()
    await register(editor, "card-collision-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    existing = (await _post(client, f"{base}/characters", {"name": "青鸿", "profile": "独有内容"}, 201)).json()
    await _invite(client, editor, project["id"], "card-collision-editor", "editor")
    async with app.state.sessionmaker() as db:
        before = await build_scope(db, project["id"], texts=("青鸿",))
        assert before.includes("character", existing["id"])
        epoch = (await db.get(Project, project["id"])).ai_history_epoch
    if version == 1:
        data = {"format": "ainovel.cards", "version": 1,
                "characters": [{"name": "幽羽", "aliases": ["青鸿"]}]}
        body = {"data": data}
    else:
        data = (await _post(client, f"{base}/cards/export", {"character_ids": [existing["id"]]}, 200)).json()
        data["characters"][0]["name"] = "幽羽"
        data["characters"][0]["aliases"] = ["青鸿"]
        preview = (await _post(editor, f"{base}/cards/import/preview", {"data": data}, 200)).json()
        body = {"data": data, "expected_fields_revision": preview["fields_revision"],
                "expected_categories_revision": preview["categories_revision"],
                "expected_ai_visibility_revision": preview["ai_visibility_revision"]}
    await _post(editor, f"{base}/cards/import", body, 200)
    async with app.state.sessionmaker() as db:
        current = await build_scope(db, project["id"], texts=("青鸿",))
        assert not current.includes("character", existing["id"])
        assert current.stamp.revision > before.stamp.revision
        assert (await db.get(Project, project["id"])).ai_history_epoch > epoch


def test_partial_word_overlap_can_change_unique_detection():
    def candidate(ident: int, name: str) -> MentionCandidate:
        return MentionCandidate(
            ref=EntityRef(kind="character", id=ident, name=name, category="character", aliases=[]),
            mode="detected", tracking=TrackingRule(), tracking_explicit=False, hidden_builtin_fields=frozenset(),
        )

    old, added = candidate(1, "坝县"), candidate(2, "阿坝")
    assert ("character", 1) in _detected([old], ("阿坝县",))
    assert ("character", 1) not in _detected([old, added], ("阿坝县",))
    assert card_service._candidate_competition([old], [old, added])


@pytest.mark.parametrize("version", [1, 6])
async def test_unrelated_card_candidate_does_not_seal_history(app, client, version):
    await register(client, "card-unrelated-owner")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    existing = (await _post(client, f"{base}/characters", {"name": "青鸿"}, 201)).json()
    async with app.state.sessionmaker() as db:
        old = (await db.get(Project, project["id"]))
        revision, epoch = old.ai_visibility_revision, old.ai_history_epoch
    if version == 1:
        data = {"format": "ainovel.cards", "version": 1, "characters": [{"name": "远行"}]}
        body = {"data": data}
    else:
        data = (await _post(client, f"{base}/cards/export", {"character_ids": [existing["id"]]}, 200)).json()
        data["characters"][0]["name"] = "远行"
        preview = (await _post(client, f"{base}/cards/import/preview", {"data": data}, 200)).json()
        body = {"data": data, "expected_fields_revision": preview["fields_revision"],
                "expected_categories_revision": preview["categories_revision"],
                "expected_ai_visibility_revision": preview["ai_visibility_revision"]}
    await _post(client, f"{base}/cards/import", body, 200)
    async with app.state.sessionmaker() as db:
        current = await db.get(Project, project["id"])
        assert current.ai_history_epoch == epoch
        assert current.ai_visibility_revision == revision + (1 if version == 6 else 0)


async def test_card_import_failure_rolls_back_already_flushed_rows(app, client, monkeypatch):
    user = await register(client, "card-atomic-owner")
    project = await create_project(client)

    async def fail_careers(*args, **kwargs):
        raise BadRequestError("模拟后续导入失败")

    monkeypatch.setattr(card_service, "_import_careers", fail_careers)
    data = CardImportRequest(data={"format": "ainovel.cards", "version": 1,
                                   "characters": [{"name": "不应保留"}]})
    async with app.state.sessionmaker() as db:
        with pytest.raises(BadRequestError):
            await card_service.import_cards(db, user["id"], project["id"], data)
        # 内部调用者捕获业务错误后提交也不能落下已经 flush 的角色或时间线。
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(CharacterCareer.id).limit(1)) is None
        assert await db.scalar(select(Project.id).where(Project.id == project["id"])) == project["id"]
        assert await db.scalar(select(Character.id).where(Character.name == "不应保留")) is None
        assert await db.scalar(select(SettingTimeline.id).where(SettingTimeline.project_id == project["id"])) is None


async def test_card_readonly_rejection_does_not_flush_pending_changes(app, client, make_client):
    await register(client, "card-flush-owner")
    viewer = make_client()
    user = await register(viewer, "card-flush-viewer")
    project = await create_project(client)
    await _invite(client, viewer, project["id"], "card-flush-viewer", "viewer")
    data = CardImportRequest(data={"format": "ainovel.cards", "version": 1})
    async with app.state.sessionmaker() as db:
        db.add(Character(project_id=project["id"], name="未授权暂存"))
        with pytest.raises(ForbiddenError):
            await card_service.import_cards(db, user["id"], project["id"], data)
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Character.id).where(Character.name == "未授权暂存")) is None


async def test_card_mapping_cannot_resolve_another_project_for_collaborator(client, make_client):
    await register(client, "card-map-owner")
    viewer, editor = make_client(), make_client()
    await register(viewer, "card-map-viewer")
    await register(editor, "card-map-editor")
    project = await create_project(client)
    foreign = await create_project(client, title="其他作品")
    base, foreign_base = f"/api/v1/projects/{project['id']}", f"/api/v1/projects/{foreign['id']}"
    foreign_world = (await _post(client, f"{foreign_base}/world", {
        "title": "不可泄露的地点", "category": "place",
    }, 201)).json()
    foreign_def = (await _post(client, f"{foreign_base}/setting-field-definitions", {
        "expected_fields_revision": 0, "label": "外部定义", "field_type": "reference",
        "applicable_types": ["character"], "reference_types": ["world.place"],
    }, 201)).json()["item"]
    await _invite(client, viewer, project["id"], "card-map-viewer", "viewer")
    await _invite(client, editor, project["id"], "card-map-editor", "editor")
    card = {"format": "ainovel.cards", "version": 4,
            "characters": [{"ref": "a", "name": "新角色",
                            "fields": [{"definition_ref": "d", "value": {"entry_ref": "x"}}]}],
            "field_definitions": [{"ref": "d", "label": "地点", "field_type": "reference",
                                   "applicable_types": ["character"], "reference_types": ["world.place"]}],
            "external_references": [{"ref": "x", "kind": "world", "type": "world.place", "name": "映射地点"}]}
    mappings = {"references": {"x": {"kind": "world", "id": foreign_world["id"]}},
                "definitions": {"d": {"action": "reuse", "target_definition_id": foreign_def["id"]}}}
    preview = await _post(viewer, f"{base}/cards/import/preview", {"data": card, "mappings": mappings}, 200)
    assert not preview.json()["can_import"]
    assert preview.json()["references"][0]["status"] == "unresolved"
    assert preview.json()["field_definitions"][0]["status"] == "incompatible"
    assert "不可泄露的地点" not in preview.text and "外部定义" not in preview.text
    await _post(editor, f"{base}/cards/import", {
        "data": card, "mappings": mappings, "expected_fields_revision": preview.json()["fields_revision"],
    }, 400)
    assert (await viewer.get(f"{base}/characters")).json() == []
