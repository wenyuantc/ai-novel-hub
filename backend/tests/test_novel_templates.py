"""个人模板 API：本人隔离、来源快照和持久建书意图。"""

import asyncio
from contextlib import suppress
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from app.models import NovelTemplate, NovelTemplateCreationReceipt, NovelTemplateImage, Project, UserQuota
from tests.helpers import create_project, register
from tests.test_setting_thumbnails import image_data

BASE = "/api/v1/novel-templates"


async def test_template_routes_are_registered_only_in_internal_api_schema(client):
    internal = (await client.get("/openapi.json")).json()
    paths = internal["paths"]
    assert {"get", "post"} <= paths[BASE].keys()
    assert {"get", "patch", "delete"} <= paths[BASE + "/{template_id}"].keys()
    assert "put" in paths[BASE + "/{template_id}/snapshot"]
    assert "post" in paths[BASE + "/{template_id}/projects"]
    assert "post" in paths["/api/v1/projects/{project_id}/novel-template-preview"]
    public = (await client.get("/api/open/v1/openapi.json")).json()
    assert all("novel-template" not in path for path in public["paths"])


async def _save(client, project_id, *, name="个人模板", selection=None):
    selection = selection or {"include_config": True}
    preview = await client.post(f"/api/v1/projects/{project_id}/novel-template-preview", json=selection)
    assert preview.status_code == 200, preview.text
    response = await client.post(BASE, json={"name": name, "description": "测试",
                                              "source_project_id": project_id, "selection": selection,
                                              "source_stamp": preview.json()["source_stamp"]})
    assert response.status_code == 201, response.text
    return response.json()


async def test_template_owner_crud_and_source_stamp(client, make_client):
    await register(client, "template-owner")
    project = await create_project(client, style="", genre="科幻")
    template = await _save(client, project["id"])
    assert template["revision"] == 1 and template["source_title"] == project["title"]
    listing = await client.get(BASE, params={"q": "模板", "page": 1, "page_size": 20})
    assert listing.status_code == 200
    assert listing.json()["total"] == 1 and listing.json()["items"][0]["id"] == template["id"]
    detail = await client.get(f"{BASE}/{template['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["include_config"] is True
    assert "snapshot" not in detail.json()
    preview_summary = await client.post(f"/api/v1/projects/{project['id']}/novel-template-preview", json={
        "include_config": True,
    })
    assert preview_summary.status_code == 200, preview_summary.text
    assert set(preview_summary.json()["included_definitions"]) == {
        "fields", "categories", "relationship_types", "careers",
    }

    async with make_client() as other:
        await register(other, "template-other")
        assert (await other.get(f"{BASE}/{template['id']}")).status_code == 404
        assert (await other.patch(f"{BASE}/{template['id']}", json={
            "expected_revision": 1, "name": "越权",
        })).status_code == 404
        assert (await other.post(f"/api/v1/projects/{project['id']}/novel-template-preview", json={
            "include_config": True,
        })).status_code == 404

    preview = (await client.post(f"/api/v1/projects/{project['id']}/novel-template-preview", json={
        "include_config": True,
    })).json()
    assert (await client.patch(f"/api/v1/projects/{project['id']}", json={"genre": "悬疑"})).status_code == 200
    stale = await client.put(f"{BASE}/{template['id']}/snapshot", json={
        "source_project_id": project["id"], "selection": {"include_config": True},
        "source_stamp": preview["source_stamp"], "expected_revision": 1,
    })
    assert stale.status_code == 409 and stale.json()["code"] == "template_source_changed"
    assert (await client.get(f"{BASE}/{template['id']}")).json()["revision"] == 1
    title_preview = (await client.post(f"/api/v1/projects/{project['id']}/novel-template-preview", json={
        "include_config": True,
    })).json()
    assert (await client.patch(f"/api/v1/projects/{project['id']}", json={"title": "新的来源书名"})).status_code == 200
    stale_title = await client.put(f"{BASE}/{template['id']}/snapshot", json={
        "source_project_id": project["id"], "selection": {"include_config": True},
        "source_stamp": title_preview["source_stamp"], "expected_revision": 1,
    })
    assert stale_title.status_code == 409 and stale_title.json()["code"] == "template_source_changed"
    renamed = await client.patch(f"{BASE}/{template['id']}", json={"name": "更新名称", "expected_revision": 1})
    assert renamed.status_code == 200 and renamed.json()["revision"] == 2
    fresh_preview = (await client.post(f"/api/v1/projects/{project['id']}/novel-template-preview", json={
        "include_config": True,
    })).json()
    replaced = await client.put(f"{BASE}/{template['id']}/snapshot", json={
        "source_project_id": project["id"], "selection": {"include_config": True},
        "source_stamp": fresh_preview["source_stamp"], "expected_revision": 2,
    })
    assert replaced.status_code == 200 and replaced.json()["revision"] == 3
    detail = (await client.get(f"{BASE}/{template['id']}")).json()
    assert detail["config_summary"]["genre"] == "悬疑" and detail["source_title"] == "新的来源书名"
    stale_delete = await client.delete(f"{BASE}/{template['id']}", params={"expected_revision": 1})
    assert stale_delete.status_code == 409 and stale_delete.json()["code"] == "stale_revision"
    assert (await client.delete(f"{BASE}/{template['id']}", params={"expected_revision": 3})).status_code == 204
    later = await _save(client, project["id"], name="后建")
    assert later["id"] > template["id"]
    assert (await client.get(f"{BASE}/{template['id']}")).status_code == 404
    assert (await client.patch(f"{BASE}/{template['id']}", json={
        "expected_revision": 1, "name": "不能命中新模板",
    })).status_code == 404
    assert (await client.delete(f"{BASE}/{template['id']}", params={"expected_revision": 1})).status_code == 404
    assert (await client.put(f"{BASE}/{template['id']}/snapshot", json={
        "source_project_id": project["id"], "selection": {"include_config": True},
        "source_stamp": fresh_preview["source_stamp"], "expected_revision": 1,
    })).status_code == 404
    assert (await client.post(f"{BASE}/{template['id']}/projects", json={
        "expected_revision": 1, "intent_key": str(uuid4()), "title": "不能命中新模板",
    })).status_code == 404
    assert (await client.get(f"{BASE}/{later['id']}")).json()["name"] == "后建"


@pytest.mark.parametrize("role", ["viewer", "editor"])
async def test_collaborator_and_admin_cannot_access_owner_templates(client, make_client, system_admin, role):
    await register(client, "private-template-owner")
    guest = make_client()
    await register(guest, "private-template-guest")
    source = await create_project(client, style="")
    source_path = f"/api/v1/projects/{source['id']}"
    invitation = await client.post(f"{source_path}/collaborators", json={
        "username": "private-template-guest", "role": role,
    })
    assert invitation.status_code == 201, invitation.text
    invite = invitation.json()
    accepted = await guest.post(f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/accept", json={
        "expected_revision": invite["revision"],
    })
    assert accepted.status_code == 200, accepted.text
    template = await _save(client, source["id"])
    selection = {"include_config": True}
    preview = await client.post(f"{source_path}/novel-template-preview", json=selection)
    assert preview.status_code == 200
    save_body = {"name": "越权快照", "source_project_id": source["id"], "selection": selection,
                 "source_stamp": preview.json()["source_stamp"]}
    replace_body = {key: value for key, value in save_body.items() if key != "name"}
    replace_body["expected_revision"] = template["revision"]
    template_path = f"{BASE}/{template['id']}"

    for actor in (guest, system_admin):
        # 两者都能看见授权作品，但作品可见不授予作者的私人模板库权限。
        assert (await actor.get(source_path)).status_code == 200
        assert (await actor.get(BASE)).json()["total"] == 0
        attempts = [
            await actor.get(template_path),
            await actor.post(f"{source_path}/novel-template-preview", json=selection),
            await actor.post(BASE, json=save_body),
            await actor.patch(template_path, json={"name": "越权", "expected_revision": 1}),
            await actor.put(f"{template_path}/snapshot", json=replace_body),
            await actor.delete(template_path, params={"expected_revision": 1}),
            await actor.post(f"{template_path}/projects", json={
                "expected_revision": 1, "intent_key": str(uuid4()), "title": "越权建书",
            }),
        ]
        assert all(response.status_code == 404 for response in attempts), [
            (response.status_code, response.text) for response in attempts
        ]
    assert (await client.get(template_path)).json()["revision"] == 1


async def test_successful_intent_replays_after_template_deleted_and_quota_exhausted(app, client):
    owner = await register(client, "template-create")
    source = await create_project(client, style="", genre="历史")
    template = await _save(client, source["id"])
    body = {"expected_revision": template["revision"], "intent_key": str(uuid4()), "title": "新作品"}
    result = await client.post(f"{BASE}/{template['id']}/projects", json=body)
    assert result.status_code == 201, result.text
    created = result.json()
    assert created["title"] == "新作品" and created["genre"] == "历史"
    assert created["chapter_count"] == 0 and created["total_words"] == 0
    async with app.state.sessionmaker() as db:
        db.add(UserQuota(user_id=owner["id"], project_mode="limited", project_value=0))
        await db.commit()
    assert (await client.delete(f"{BASE}/{template['id']}", params={"expected_revision": 1})).status_code == 204
    replay = await client.post(f"{BASE}/{template['id']}/projects", json=body)
    assert replay.status_code == 201 and replay.json()["id"] == created["id"]
    conflict = await client.post(f"{BASE}/{template['id']}/projects", json={**body, "title": "另一书"})
    assert conflict.status_code == 409 and conflict.json()["code"] == "creation_intent_conflict"
    assert (await client.delete(f"/api/v1/projects/{created['id']}")).status_code == 204
    unavailable = await client.post(f"{BASE}/{template['id']}/projects", json=body)
    assert unavailable.status_code == 409 and unavailable.json()["code"] == "creation_result_unavailable"


async def test_created_project_applies_overrides_and_only_selected_owned_pen_name(app, client, make_client):
    owner = await register(client, "template-pen-owner")
    old_pen = await client.post("/api/v1/pen-names", json={"name": "原作笔名"})
    chosen = await client.post("/api/v1/pen-names", json={"name": "新书署名"})
    assert old_pen.status_code == chosen.status_code == 201
    source = await create_project(client, style="", genre="来源题材", target_words=20_000,
                                  pen_name_id=old_pen.json()["id"])
    template = await _save(client, source["id"])
    path = f"{BASE}/{template['id']}/projects"
    async with make_client() as other:
        await register(other, "template-pen-other")
        foreign = await other.post("/api/v1/pen-names", json={"name": "他人笔名"})
        assert foreign.status_code == 201
        denied_key = str(uuid4())
        denied = await client.post(path, json={"expected_revision": 1, "intent_key": denied_key,
                                               "title": "不能留下的新书", "pen_name_id": foreign.json()["id"]})
        assert denied.status_code == 404
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == owner["id"])) == 1
            assert await db.scalar(select(func.count(NovelTemplateCreationReceipt.id)).where(
                NovelTemplateCreationReceipt.intent_key == denied_key,
            )) == 0
    created = await client.post(path, json={
        "expected_revision": 1, "intent_key": str(uuid4()), "title": "新标题",
        "pen_name_id": chosen.json()["id"], "overrides": {"genre": "新题材", "target_words": None},
    })
    assert created.status_code == 201, created.text
    assert created.json()["genre"] == "新题材" and created.json()["target_words"] is None
    assert created.json()["pen_name"] == {"id": chosen.json()["id"], "name": "新书署名"}
    without_pen = await client.post(path, json={
        "expected_revision": 1, "intent_key": str(uuid4()), "title": "无署名",
    })
    assert without_pen.status_code == 201 and without_pen.json()["pen_name"] is None
    assert without_pen.json()["genre"] == "来源题材" and without_pen.json()["target_words"] == 20_000


async def test_lost_response_after_commit_replays_success_receipt(app, client, monkeypatch):
    from app.services import project_service

    owner = await register(client, "template-lost-response")
    source = await create_project(client, style="")
    template = await _save(client, source["id"])
    body = {"expected_revision": 1, "intent_key": str(uuid4()), "title": "提交成功但响应丢失"}
    original = project_service.project_out
    committed: dict[str, int] = {}

    async def fail_response_once(db, project, *args, **kwargs):
        if not committed:
            committed["project_id"] = project.id
            raise RuntimeError("模拟提交后响应中断")
        return await original(db, project, *args, **kwargs)

    monkeypatch.setattr(project_service, "project_out", fail_response_once)
    path = f"{BASE}/{template['id']}/projects"
    with pytest.raises(RuntimeError, match="模拟提交后响应中断"):
        await client.post(path, json=body)
    async with app.state.sessionmaker() as db:
        assert await db.get(Project, committed["project_id"]) is not None
        assert await db.scalar(select(func.count(NovelTemplateCreationReceipt.id)).where(
            NovelTemplateCreationReceipt.intent_key == body["intent_key"],
        )) == 1
    replay = await client.post(path, json=body)
    assert replay.status_code == 201 and replay.json()["id"] == committed["project_id"]
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == owner["id"])) == 2


async def test_unknown_commit_result_replays_the_committed_receipt(app, client, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services import novel_template_apply

    owner = await register(client, "template-uncertain-commit")
    source = await create_project(client, style="")
    template = await _save(client, source["id"])
    body = {"expected_revision": 1, "intent_key": str(uuid4()), "title": "提交确认丢失"}
    apply = novel_template_apply.apply_template
    commit = AsyncSession.commit
    failed = False

    async def mark_creation(db, project, snapshot, images):
        await apply(db, project, snapshot, images)
        db.info["simulate_template_commit_unknown"] = True

    async def commit_then_lose_ack(db):
        nonlocal failed
        marked = db.info.pop("simulate_template_commit_unknown", False)
        # 先真实提交全部业务和收据，再模拟数据库客户端未收到提交确认。
        await commit(db)
        if marked and not failed:
            failed = True
            raise ConnectionError("模拟提交确认丢失")

    monkeypatch.setattr(novel_template_apply, "apply_template", mark_creation)
    monkeypatch.setattr(AsyncSession, "commit", commit_then_lose_ack)
    path = f"{BASE}/{template['id']}/projects"
    with pytest.raises(ConnectionError, match="提交确认丢失"):
        await client.post(path, json=body)
    async with app.state.sessionmaker() as db:
        receipt = await db.scalar(select(NovelTemplateCreationReceipt).where(
            NovelTemplateCreationReceipt.owner_id == owner["id"],
            NovelTemplateCreationReceipt.intent_key == body["intent_key"],
        ))
        assert receipt is not None and receipt.result_project_id is not None
        created_id = receipt.result_project_id
    replay = await client.post(path, json=body)
    assert replay.status_code == 201 and replay.json()["id"] == created_id
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == owner["id"])) == 2


async def test_intent_replay_follows_recycle_restore_and_purge(app, client, recycle_admin):
    await register(client, "intent-lifecycle")
    source = await create_project(client, style="")
    template = await _save(client, source["id"])
    body = {"expected_revision": 1, "intent_key": str(uuid4()), "title": "收据作品"}
    created_response = await client.post(f"{BASE}/{template['id']}/projects", json=body)
    assert created_response.status_code == 201, created_response.text
    created_id = created_response.json()["id"]
    assert (await client.delete(f"/api/v1/projects/{created_id}")).status_code == 204
    unavailable = await client.post(f"{BASE}/{template['id']}/projects", json=body)
    assert unavailable.status_code == 409 and unavailable.json()["code"] == "creation_result_unavailable"
    page = await recycle_admin.get("/api/v1/admin/recycle-bin/projects")
    record = next(item for item in page.json()["items"] if item["id"] == created_id)
    restored = await recycle_admin.post(f"/api/v1/admin/recycle-bin/projects/{created_id}/restore", json={
        "deleted_at": record["deleted_at"], "confirm": True,
    })
    assert restored.status_code == 204, restored.text
    replay = await client.post(f"{BASE}/{template['id']}/projects", json=body)
    assert replay.status_code == 201 and replay.json()["id"] == created_id
    assert (await client.delete(f"/api/v1/projects/{created_id}")).status_code == 204
    page = await recycle_admin.get("/api/v1/admin/recycle-bin/projects")
    record = next(item for item in page.json()["items"] if item["id"] == created_id)
    purged = await recycle_admin.post(f"/api/v1/admin/recycle-bin/projects/{created_id}/purge", json={
        "deleted_at": record["deleted_at"], "confirm": True, "title": "收据作品",
    })
    assert purged.status_code == 204, purged.text
    unavailable = await client.post(f"{BASE}/{template['id']}/projects", json=body)
    assert unavailable.status_code == 409 and unavailable.json()["code"] == "creation_result_unavailable"
    async with app.state.sessionmaker() as db:
        receipt = await db.scalar(select(NovelTemplateCreationReceipt).where(
            NovelTemplateCreationReceipt.intent_key == body["intent_key"],
        ))
        assert receipt is not None and receipt.result_project_id is None


async def test_source_preview_does_not_cross_recycle_and_restore(client, recycle_admin):
    await register(client, "template-source-lifecycle")
    source = await create_project(client, style="")
    base = f"/api/v1/projects/{source['id']}"
    selection = {"include_config": True}
    preview = await client.post(f"{base}/novel-template-preview", json=selection)
    assert preview.status_code == 200, preview.text
    assert (await client.delete(base)).status_code == 204
    records = await recycle_admin.get("/api/v1/admin/recycle-bin/projects")
    record = next(item for item in records.json()["items"] if item["id"] == source["id"])
    restored = await recycle_admin.post(f"/api/v1/admin/recycle-bin/projects/{source['id']}/restore", json={
        "deleted_at": record["deleted_at"], "confirm": True,
    })
    assert restored.status_code == 204, restored.text
    save = await client.post(BASE, json={"name": "旧预览", "source_project_id": source["id"],
                                         "selection": selection, "source_stamp": preview.json()["source_stamp"]})
    assert save.status_code == 409 and save.json()["code"] == "template_source_changed"


async def test_quota_failure_and_last_step_failure_are_atomic(app, client, monkeypatch):
    from app.services import novel_template_apply

    owner = await register(client, "intent-quota")
    source = await create_project(client, style="")
    template = await _save(client, source["id"])
    body = {"expected_revision": 1, "intent_key": str(uuid4()), "title": "不应残留"}
    async with app.state.sessionmaker() as db:
        db.add(UserQuota(user_id=owner["id"], project_mode="limited", project_value=1))
        await db.commit()
    rejected = await client.post(f"{BASE}/{template['id']}/projects", json=body)
    assert rejected.status_code == 409 and rejected.json()["code"] == "quota_exceeded"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == owner["id"])) == 1
        assert await db.scalar(select(func.count(NovelTemplateCreationReceipt.id))) == 0
        quota = await db.get(UserQuota, owner["id"])
        quota.project_mode, quota.project_value = "unlimited", None
        await db.commit()
    original = novel_template_apply.apply_template

    async def fail_after_apply(db, project, snapshot, images):
        await original(db, project, snapshot, images)
        await db.flush()
        raise RuntimeError("图片应用之后注入失败")

    monkeypatch.setattr(novel_template_apply, "apply_template", fail_after_apply)
    with pytest.raises(RuntimeError, match="图片应用之后注入失败"):
        await client.post(f"{BASE}/{template['id']}/projects", json=body)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == owner["id"])) == 1
        assert await db.scalar(select(func.count(NovelTemplateCreationReceipt.id))) == 0
        assert await db.get(NovelTemplate, template["id"]) is not None


async def test_cancellation_after_real_image_apply_rolls_back_everything(app, client, monkeypatch):
    from app.models import SettingImageResource
    from app.services import novel_template_apply

    owner = await register(client, "template-cancellation")
    source = await create_project(client, style="")
    base = f"/api/v1/projects/{source['id']}"
    character = await client.post(f"{base}/characters", json={"name": "带图角色"})
    assert character.status_code == 201, character.text
    cid = character.json()["id"]
    uploaded = await client.put(f"{base}/settings/character/{cid}/thumbnail", data={"expected_version": ""},
                                files={"file": ("a.png", image_data(), "image/png")})
    assert uploaded.status_code == 200, uploaded.text
    template = await _save(client, source["id"], selection={"include_config": True, "character_ids": [cid]})
    original = novel_template_apply.apply_template
    reached = asyncio.Event()
    created: dict[str, int] = {}

    async def cancel_after_apply(db, project, snapshot, images):
        await original(db, project, snapshot, images)
        await db.flush()
        assert await db.scalar(select(func.count(SettingImageResource.id)).where(
            SettingImageResource.project_id == project.id,
        )) == 1
        created["project_id"] = project.id
        reached.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(novel_template_apply, "apply_template", cancel_after_apply)
    task = asyncio.create_task(client.post(f"{BASE}/{template['id']}/projects", json={
        "expected_revision": 1, "intent_key": str(uuid4()), "title": "取消应回滚",
    }))
    try:
        await asyncio.wait_for(reached.wait(), 10)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        async with app.state.sessionmaker() as db:
            assert await db.get(Project, created["project_id"]) is None
            assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == owner["id"])) == 1
            assert await db.scalar(select(func.count(SettingImageResource.id)).where(
                SettingImageResource.project_id == created["project_id"],
            )) == 0
            assert await db.scalar(select(func.count(NovelTemplateCreationReceipt.id))) == 0
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def test_snapshot_storage_preflight_preserves_metadata_management(app, client, monkeypatch):
    from app.services import novel_template_service

    owner = await register(client, "template-json-bounds")
    source = await create_project(client, style="")
    template = await _save(client, source["id"])
    monkeypatch.setattr(novel_template_service, "_MAX_JSON_STORAGE_BYTES", 10)
    listing = await client.get(BASE)
    assert listing.status_code == 200 and listing.json()["total"] == 1
    changed = await client.patch(f"{BASE}/{template['id']}", json={"name": "仍可管理", "expected_revision": 1})
    assert changed.status_code == 200 and changed.json()["revision"] == 2
    detail = await client.get(f"{BASE}/{template['id']}")
    assert detail.status_code == 400 and detail.json()["code"] == "template_too_large"
    create = await client.post(f"{BASE}/{template['id']}/projects", json={
        "expected_revision": 2, "intent_key": str(uuid4()), "title": "不能加载超限材料",
    })
    assert create.status_code == 400 and create.json()["code"] == "template_too_large"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == owner["id"])) == 1
        assert await db.scalar(select(func.count(NovelTemplateCreationReceipt.id))) == 0
    assert (await client.delete(f"{BASE}/{template['id']}", params={"expected_revision": 2})).status_code == 204


@pytest.mark.parametrize("stored", ["{broken-json", "[" * 1100 + "0" + "]" * 1100, b"\xff", "[]", "null"])
async def test_invalid_stored_json_rejects_detail_and_creation_without_side_effects(app, client, stored):
    owner = await register(client, "template-corrupt-json")
    source = await create_project(client, style="")
    template = await _save(client, source["id"])
    async with app.state.sessionmaker() as db:
        # SQLite 可以存入非 JSON 文本或非法 UTF-8，不能依赖 JSON ORM 自动解码成功。
        await db.execute(text("UPDATE novel_templates SET snapshot = :snapshot WHERE id = :id"), {
            "snapshot": stored, "id": template["id"],
        })
        await db.commit()
    assert (await client.get(BASE)).json()["total"] == 1
    detail = await client.get(f"{BASE}/{template['id']}")
    assert detail.status_code == 400 and detail.json()["code"] == "invalid_novel_template"
    create = await client.post(f"{BASE}/{template['id']}/projects", json={
        "expected_revision": 1, "intent_key": str(uuid4()), "title": "损坏模板不能创建",
    })
    assert create.status_code == 400 and create.json()["code"] == "invalid_novel_template"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == owner["id"])) == 1
        assert await db.scalar(select(func.count(NovelTemplateCreationReceipt.id))) == 0


async def test_template_image_is_private_bounded_and_not_trusting_metadata(app, client, make_client):
    owner = await register(client, "image-template-owner")
    source = await create_project(client, style="")
    base = f"/api/v1/projects/{source['id']}"
    character = await client.post(f"{base}/characters", json={"name": "图中人物"})
    assert character.status_code == 201, character.text
    cid = character.json()["id"]
    upload = await client.put(f"{base}/settings/character/{cid}/thumbnail", data={"expected_version": ""},
                              files={"file": ("image.png", image_data(), "image/png")})
    assert upload.status_code == 200, upload.text
    template = await _save(client, source["id"], selection={"include_config": True, "character_ids": [cid]})
    detail = await client.get(f"{BASE}/{template['id']}")
    assert detail.status_code == 200, detail.text
    image = detail.json()["contents"]["characters"][0]
    assert image["has_image"] is True
    image_path = f"{BASE}/{template['id']}/images/{image['ref']}?revision=1"
    response = await client.get(image_path)
    assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
    assert (await client.get(image_path, headers={"If-None-Match": response.headers["etag"]})).status_code == 304
    async with make_client() as other:
        await register(other, "image-template-other")
        assert (await other.get(image_path)).status_code == 404
        assert (await other.get(image_path, headers={"If-None-Match": response.headers["etag"]})).status_code == 404
    renamed = await client.patch(f"{BASE}/{template['id']}", json={"name": "改名后的模板", "expected_revision": 1})
    assert renamed.status_code == 200 and renamed.json()["revision"] == 2
    assert (await client.get(image_path, headers={"If-None-Match": response.headers["etag"]})).status_code == 404
    image_path = f"{BASE}/{template['id']}/images/{image['ref']}?revision=2"
    async with app.state.sessionmaker() as db:
        image_row = await db.scalar(select(NovelTemplateImage).where(NovelTemplateImage.template_id == template["id"]))
        image_row.data = b"x" * (512 * 1024 + 1)
        await db.commit()
    invalid = await client.get(image_path)
    assert invalid.status_code == 400 and invalid.json()["code"] == "invalid_novel_template"
    failed = await client.post(f"{BASE}/{template['id']}/projects", json={
        "expected_revision": 2, "intent_key": str(uuid4()), "title": "不能创建",
    })
    assert failed.status_code == 400 and failed.json()["code"] == "invalid_novel_template"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == owner["id"])) == 1
