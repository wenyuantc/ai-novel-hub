"""在隔离 PostgreSQL schema 验证模板实体、事务回滚及风格并发。"""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError
from app.models import (
    AdminAuditLog,
    AnalysisChange,
    ChapterAnalysis,
    Project,
    SettingCategory,
    SettingCategoryRule,
    SettingFieldDefinition,
    SettingFieldOption,
    SettingProgression,
    SettingReferenceHandle,
    SettingTextField,
    SettingTimeline,
    User,
    WritingStyle,
)
from app.services import project_lifecycle, recycle_bin_service, story_service
from app.services.novel_template_apply import apply_template
from tests.helpers import create_project, register
from tests.test_novel_template_apply import (
    _style_snapshot,
    _style_values,
)
from tests.test_novel_template_apply import (
    test_apply_builds_confirmed_independent_graph_and_empty_body as _full_graph,
)
from tests.test_novel_template_apply import (
    test_apply_failure_at_image_stage_rolls_back_entire_new_project as _rollback,
)
from tests.test_novel_template_apply import (
    test_config_only_preserves_definitions_and_categories_without_entries as _config_only,
)
from tests.test_novel_template_apply import (
    test_partial_outline_volumes_round_trip_through_template_api as _outline_api_round_trip,
)
from tests.test_novel_template_apply import (
    test_partial_outlines_keep_order_with_new_consecutive_chapters_and_plans as _partial_outlines,
)
from tests.test_novel_template_apply import (
    test_relationship_type_collision_aborts_before_inserting_types as _type_collision,
)
from tests.test_novel_template_apply import (
    test_relationship_type_frozen_semantics_and_direction as _type_semantics,
)
from tests.test_novel_template_apply import (
    test_round_trip_planned_world_and_required_field_without_configuration as _round_trip,
)
from tests.test_series_postgresql import _wait_for_blocker
from tests.test_setting_fields_postgresql import postgres_fields_app as postgres_fields_app
from tests.test_setting_fields_postgresql import postgres_fields_client as postgres_fields_client


async def test_postgresql_complete_graph(postgres_fields_app, postgres_fields_client):
    await _full_graph(postgres_fields_app, postgres_fields_client)


async def test_postgresql_late_failure_rolls_back(postgres_fields_app, postgres_fields_client, monkeypatch):
    await _rollback(postgres_fields_app, postgres_fields_client, monkeypatch)


async def test_postgresql_planned_value_round_trip(postgres_fields_app, postgres_fields_client):
    await _round_trip(postgres_fields_app, postgres_fields_client)


async def test_postgresql_config_only(postgres_fields_app, postgres_fields_client):
    await _config_only(postgres_fields_app, postgres_fields_client)


async def test_postgresql_partial_outlines_renumber_volumes(postgres_fields_app, postgres_fields_client):
    await _partial_outlines(postgres_fields_app, postgres_fields_client)


async def test_postgresql_partial_outlines_template_api(postgres_fields_app, postgres_fields_client):
    await _outline_api_round_trip(postgres_fields_app, postgres_fields_client)


@pytest.mark.parametrize("mode", ["same_global", "changed_global", "project"])
async def test_postgresql_relationship_type_frozen_semantics(postgres_fields_app, postgres_fields_client, mode):
    await _type_semantics(postgres_fields_app, postgres_fields_client, mode)


async def test_postgresql_relationship_type_collision(postgres_fields_app, postgres_fields_client):
    await _type_collision(postgres_fields_app, postgres_fields_client)


async def _seed_setting_history(app, project_id, name):
    async with app.state.sessionmaker.begin() as db:
        character = await story_service.new_character(db, project_id, {"name": name})
        timeline = await db.scalar(select(SettingTimeline).where(
            SettingTimeline.project_id == project_id, SettingTimeline.character_id == character.id))
        definition = SettingFieldDefinition(project_id=project_id, label="记录", field_type="text",
                                            applicable_types=["character"], reference_types=[])
        db.add(definition)
        await db.flush()
        field = SettingTextField(timeline_id=timeline.id, definition_id=definition.id, base_value="原始基线")
        db.add(field)
        await db.flush()
        event = SettingProgression(timeline_id=timeline.id, custom_field_id=field.id, operation="replace",
                                   value="计划后的值", anchor_kind="planned", chapter_no=3, boundary="start")
        db.add(event)
        await db.flush()
        return {"character": character.id, "timeline": timeline.id, "definition": definition.id,
                "field": field.id, "event": event.id}


@asynccontextmanager
async def _admin(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        user = await register(client, "recycle_manager")
        async with app.state.sessionmaker.begin() as db:
            (await db.get(User, user["id"])).is_admin = True
        yield client


async def _history_state(db, ids):
    field = await db.get(SettingTextField, ids["field"])
    event = await db.get(SettingProgression, ids["event"])
    timeline = await db.get(SettingTimeline, ids["timeline"])
    return (field.definition_id, field.base_value, event.timeline_id, event.custom_field_id,
            event.value, event.superseded_by_id, event.anchor_kind, event.chapter_id, event.chapter_no,
            event.scene_id, event.source_change_id, timeline.character_id)


@pytest.mark.parametrize("broken", ["field_out", "field_in", "event_out", "event_in",
                                      "anchor_in", "reference_in", "subject_in", "rule_in", "rule_out", "option_in"])
async def test_postgresql_purge_rejects_cross_project_history_without_changes(postgres_fields_app,
                                                                            postgres_fields_client, broken):
    app, client = postgres_fields_app, postgres_fields_client
    await register(client)
    target = await create_project(client, title="待彻删作品")
    other = await create_project(client, title="另一部作品")
    owned = await _seed_setting_history(app, target["id"], "待删角色")
    survivor = await _seed_setting_history(app, other["id"], "保留角色")
    chapter_id = None
    if broken == "anchor_in":
        chapter = await client.post(f"/api/v1/projects/{target['id']}/chapters", json={"title": "不可跨作品锚定"})
        assert chapter.status_code == 201, chapter.text
        chapter_id = chapter.json()["id"]
    async with app.state.sessionmaker.begin() as db:
        if broken in {"rule_in", "rule_out", "option_in"}:
            category_project = target["id"] if broken == "rule_out" else other["id"]
            definition_id = owned["definition"] if broken == "rule_in" else survivor["definition"]
            option_definition_id = owned["definition"] if broken == "option_in" else definition_id
            category = SettingCategory(project_id=category_project, name="跨书规则", applicable_types=["character"],
                                       match_mode="all", sort=0)
            option = SettingFieldOption(definition_id=option_definition_id, label="异常选项")
            db.add_all([category, option])
            await db.flush()
            rule = SettingCategoryRule(category_id=category.id, kind="dropdown", sort=0,
                                       definition_id=definition_id, option_id=option.id)
            db.add(rule)
        if broken == "field_out":
            await db.execute(update(SettingTextField).where(SettingTextField.id == owned["field"])
                             .values(definition_id=survivor["definition"]))
        elif broken == "field_in":
            await db.execute(update(SettingTextField).where(SettingTextField.id == survivor["field"])
                             .values(definition_id=owned["definition"]))
        elif broken == "event_out":
            await db.execute(update(SettingProgression).where(SettingProgression.id == owned["event"])
                             .values(custom_field_id=survivor["field"]))
        elif broken == "event_in":
            await db.execute(update(SettingProgression).where(SettingProgression.id == survivor["event"])
                             .values(custom_field_id=owned["field"]))
        elif broken == "anchor_in":
            await db.execute(update(SettingProgression).where(SettingProgression.id == survivor["event"])
                             .values(anchor_kind="chapter", chapter_id=chapter_id, chapter_no=None))
        elif broken == "reference_in":
            handle = SettingReferenceHandle(project_id=other["id"], kind="character",
                                            character_id=owned["character"])
            db.add(handle)
        elif broken == "subject_in":
            await db.execute(update(SettingTimeline).where(SettingTimeline.id == survivor["timeline"])
                             .values(character_id=owned["character"]))
    async with app.state.sessionmaker() as db:
        before_owned = await _history_state(db, owned)
        before_other = await _history_state(db, survivor)
        before_other_subject = (await db.get(SettingTimeline, survivor["timeline"])).character_id
    assert (await client.delete(f"/api/v1/projects/{target['id']}")).status_code == 204
    async with _admin(app) as admin:
        listing = await admin.get("/api/v1/admin/recycle-bin/projects")
        row = next(item for item in listing.json()["items"] if item["id"] == target["id"])
        response = await admin.post(f"/api/v1/admin/recycle-bin/projects/{target['id']}/purge",
                                    json={"deleted_at": row["deleted_at"], "title": row["title"], "confirm": True})
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "invalid_setting_purge_graph"
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, target["id"])).deleted_at is not None
        assert await _history_state(db, owned) == before_owned
        assert await _history_state(db, survivor) == before_other
        assert (await db.get(SettingTimeline, survivor["timeline"])).character_id == before_other_subject
        audits = list(await db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "project.purge")))
        assert [item.result for item in audits] == ["failure"]


async def test_postgresql_expired_purge_keeps_other_history_and_definition_guard(postgres_fields_app,
                                                                               postgres_fields_client):
    app, client = postgres_fields_app, postgres_fields_client
    await register(client)
    target = await create_project(client, title="到期作品")
    other = await create_project(client, title="保留作品")
    owned = await _seed_setting_history(app, target["id"], "待删角色")
    survivor = await _seed_setting_history(app, other["id"], "保留角色")
    async with app.state.sessionmaker.begin() as db:
        newer = SettingProgression(timeline_id=owned["timeline"], custom_field_id=owned["field"],
                                   operation="replace", value="替代事件", anchor_kind="planned", chapter_no=4,
                                   boundary="start")
        db.add(newer)
        await db.flush()
        newer_id = newer.id
        await db.execute(update(SettingProgression).where(SettingProgression.id == owned["event"])
                         .values(superseded_by_id=newer_id, active=False))
    async with app.state.sessionmaker() as db:
        before_other = await _history_state(db, survivor)
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(delete(SettingFieldDefinition).where(
                    SettingFieldDefinition.id == owned["definition"]))
    assert (await client.delete(f"/api/v1/projects/{target['id']}")).status_code == 204
    fixed = project_lifecycle.now_utc()
    async with app.state.sessionmaker.begin() as db:
        (await db.get(Project, target["id"])).deleted_at = fixed - timedelta(days=31)
    assert await recycle_bin_service.purge_expired(app.state.task_engine, now=fixed) == 1
    async with app.state.sessionmaker() as db:
        assert await db.get(Project, target["id"]) is None
        assert await db.get(SettingTextField, owned["field"]) is None
        assert await db.get(SettingProgression, owned["event"]) is None
        assert await db.get(SettingProgression, newer_id) is None
        assert await _history_state(db, survivor) == before_other
        audit = await db.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "project.expire"))
        assert audit.result == "success" and audit.actor_user_id is None


@pytest.mark.parametrize("broken", ["anchor", "subject", "reference"])
async def test_postgresql_empty_catalog_purge_rejects_incoming_foreign_history(
    postgres_fields_app, postgres_fields_client, broken,
):
    app, client = postgres_fields_app, postgres_fields_client
    await register(client)
    target = await create_project(client, title="没有字段目录")
    other = await create_project(client, title="引用了目标的作品")
    survivor = await _seed_setting_history(app, other["id"], "保留角色")
    async with app.state.sessionmaker.begin() as db:
        character = await story_service.new_character(db, target["id"], {"name": "空目录角色"})
        target_character_id = character.id
    if broken == "anchor":
        chapter = await client.post(f"/api/v1/projects/{target['id']}/chapters", json={"title": "他书错误锚点"})
        assert chapter.status_code == 201, chapter.text
    async with app.state.sessionmaker.begin() as db:
        if broken == "anchor":
            await db.execute(update(SettingProgression).where(SettingProgression.id == survivor["event"])
                             .values(anchor_kind="chapter", chapter_id=chapter.json()["id"], chapter_no=None))
        elif broken == "subject":
            await db.execute(update(SettingTimeline).where(SettingTimeline.id == survivor["timeline"])
                             .values(character_id=target_character_id))
        else:
            handle = SettingReferenceHandle(project_id=other["id"], kind="character",
                                            character_id=target_character_id)
            db.add(handle)
    async with app.state.sessionmaker() as db:
        before = await _history_state(db, survivor)
    assert (await client.delete(f"/api/v1/projects/{target['id']}")).status_code == 204
    async with _admin(app) as admin:
        listing = await admin.get("/api/v1/admin/recycle-bin/projects")
        row = next(item for item in listing.json()["items"] if item["id"] == target["id"])
        response = await admin.post(f"/api/v1/admin/recycle-bin/projects/{target['id']}/purge",
                                    json={"deleted_at": row["deleted_at"], "title": row["title"], "confirm": True})
        assert response.status_code == 409 and response.json()["code"] == "invalid_setting_purge_graph"
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, target["id"])).deleted_at is not None
        assert await _history_state(db, survivor) == before


@pytest.mark.parametrize("with_fields", [False, True])
@pytest.mark.parametrize("edge", ["chapter", "scene", "subject", "analysis"])
async def test_postgresql_purge_blocks_new_foreign_references_after_preflight(
    postgres_fields_app, postgres_fields_client, monkeypatch, with_fields, edge,
):
    app, client = postgres_fields_app, postgres_fields_client
    await register(client)
    target = await create_project(client, title="并发章锚目标")
    other = await create_project(client, title="并发章锚其他作品")
    if with_fields:
        owned = await _seed_setting_history(app, target["id"], "目标角色")
        target_character_id = owned["character"]
    else:
        async with app.state.sessionmaker.begin() as db:
            target_character_id = (await story_service.new_character(db, target["id"], {"name": "目标角色"})).id
    survivor = await _seed_setting_history(app, other["id"], "保留角色")
    chapter = await client.post(f"/api/v1/projects/{target['id']}/chapters", json={"title": "目标章"})
    assert chapter.status_code == 201, chapter.text
    chapter_id = chapter.json()["id"]
    scenes = await client.get(f"/api/v1/projects/{target['id']}/chapters/{chapter_id}/scenes")
    assert scenes.status_code == 200, scenes.text
    scene_id = scenes.json()["items"][0]["id"]
    async with app.state.sessionmaker.begin() as db:
        analysis = ChapterAnalysis(project_id=target["id"], chapter_id=chapter_id, chapter_no=1,
                                   content_hash="a" * 64)
        db.add(analysis)
        await db.flush()
        change = AnalysisChange(project_id=target["id"], analysis_id=analysis.id, chapter_no=1,
                                kind="character", status="applied")
        db.add(change)
        await db.flush()
        change_id = change.id
    assert (await client.delete(f"/api/v1/projects/{target['id']}")).status_code == 204
    async with app.state.sessionmaker() as db:
        before = await _history_state(db, survivor)
        deleted_at = (await db.get(Project, target["id"])).deleted_at

    checked = asyncio.Event()
    resume = asyncio.Event()
    holder_pid = None
    original = recycle_bin_service._has_unsafe_setting_references

    async def gate(db, project_id, **kwargs):
        nonlocal holder_pid
        result = await original(db, project_id, **kwargs)
        if project_id == target["id"]:
            holder_pid = await db.scalar(text("select pg_backend_pid()"))
            checked.set()
            await asyncio.wait_for(resume.wait(), 10)
        return result

    monkeypatch.setattr(recycle_bin_service, "_has_unsafe_setting_references", gate)

    async def purge():
        async with app.state.sessionmaker() as db:
            await recycle_bin_service.purge(db, app.state.task_engine, None, target["id"], deleted_at,
                                            cutoff=project_lifecycle.now_utc() + timedelta(days=31))

    async def create_foreign_anchor():
        async with app.state.sessionmaker() as db:
            if edge == "subject":
                await db.execute(update(SettingTimeline).where(SettingTimeline.id == survivor["timeline"])
                                 .values(character_id=target_character_id))
            else:
                changes = {
                    "chapter": {"anchor_kind": "chapter", "chapter_id": chapter_id, "chapter_no": None},
                    "scene": {"anchor_kind": "scene", "scene_id": scene_id, "chapter_no": None},
                    "analysis": {"source_change_id": change_id},
                }
                await db.execute(update(SettingProgression).where(SettingProgression.id == survivor["event"])
                                 .values(**changes[edge]))
            await db.commit()

    purging = asyncio.create_task(purge())
    writer = blocker = None
    try:
        await asyncio.wait_for(checked.wait(), 10)
        writer = asyncio.create_task(create_foreign_anchor())
        blocker = asyncio.create_task(_wait_for_blocker(app.state.sessionmaker, holder_pid))
        completed, _ = await asyncio.wait({writer, blocker}, timeout=8, return_when=asyncio.FIRST_COMPLETED)
        did_block = blocker in completed and writer not in completed
    finally:
        resume.set()
        await asyncio.wait_for(purging, 10)
        if blocker is not None:
            blocker.cancel()
            with suppress(asyncio.CancelledError):
                await blocker
    result = (await asyncio.gather(writer, return_exceptions=True))[0]
    assert did_block, f"他书并发写入未等待目标行锁: {result!r}"
    assert isinstance(result, IntegrityError)
    async with app.state.sessionmaker() as db:
        assert await db.get(Project, target["id"]) is None
        assert await _history_state(db, survivor) == before


@pytest.mark.parametrize("with_fields", [False, True])
async def test_postgresql_purge_waits_for_existing_foreign_fk_then_rechecks(
    postgres_fields_app, postgres_fields_client, with_fields,
):
    app, client = postgres_fields_app, postgres_fields_client
    await register(client)
    target = await create_project(client, title="等待章锚写入")
    other = await create_project(client, title="正在修改章锚的作品")
    if with_fields:
        await _seed_setting_history(app, target["id"], "目标角色")
    survivor = await _seed_setting_history(app, other["id"], "保留角色")
    chapter = await client.post(f"/api/v1/projects/{target['id']}/chapters", json={"title": "目标章"})
    assert chapter.status_code == 201, chapter.text
    chapter_id = chapter.json()["id"]
    assert (await client.delete(f"/api/v1/projects/{target['id']}")).status_code == 204
    async with app.state.sessionmaker() as db:
        deleted_at = (await db.get(Project, target["id"])).deleted_at

    async def purge():
        async with app.state.sessionmaker() as db:
            await recycle_bin_service.purge(db, app.state.task_engine, None, target["id"], deleted_at,
                                            cutoff=project_lifecycle.now_utc() + timedelta(days=31))

    purging = None
    try:
        async with app.state.sessionmaker() as writer:
            # 此直接写入构造服务层通常拒绝的跨作品 FK；执行完成后事务仍持有章行 KEY SHARE。
            await writer.execute(update(SettingProgression).where(SettingProgression.id == survivor["event"])
                                 .values(anchor_kind="chapter", chapter_id=chapter_id, chapter_no=None))
            expected = await _history_state(writer, survivor)
            writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
            purging = asyncio.create_task(purge())
            await asyncio.wait_for(_wait_for_blocker(app.state.sessionmaker, writer_pid), 10)
            assert not purging.done()
            await writer.commit()
        with pytest.raises(ConflictError) as error:
            await asyncio.wait_for(purging, 10)
        assert error.value.extra["code"] == "invalid_setting_purge_graph"
    finally:
        if purging is not None and not purging.done():
            purging.cancel()
            with suppress(asyncio.CancelledError):
                await purging

    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, target["id"])).deleted_at is not None
        assert await _history_state(db, survivor) == expected
        assert await db.scalar(select(func.count()).select_from(AdminAuditLog).where(
            AdminAuditLog.action == "project.expire", AdminAuditLog.result == "success")) == 0


async def test_postgresql_failure_after_history_cleanup_rolls_back_purge_and_success_audit(
    postgres_fields_app, postgres_fields_client, monkeypatch,
):
    app, client = postgres_fields_app, postgres_fields_client
    await register(client)
    target = await create_project(client, title="失败后仍在回收站")
    owned = await _seed_setting_history(app, target["id"], "待删角色")
    async with app.state.sessionmaker() as db:
        before = await _history_state(db, owned)
    assert (await client.delete(f"/api/v1/projects/{target['id']}")).status_code == 204
    original = recycle_bin_service._purge_setting_history

    async def fail_after_cleanup(*args):
        await original(*args)
        raise RuntimeError("设定历史清理后注入错误")

    monkeypatch.setattr(recycle_bin_service, "_purge_setting_history", fail_after_cleanup)
    async with _admin(app) as admin:
        listing = await admin.get("/api/v1/admin/recycle-bin/projects")
        row = next(item for item in listing.json()["items"] if item["id"] == target["id"])
        response = await admin.post(f"/api/v1/admin/recycle-bin/projects/{target['id']}/purge",
                                    json={"deleted_at": row["deleted_at"], "title": row["title"], "confirm": True})
        assert response.status_code == 500
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, target["id"])).deleted_at is not None
        assert await _history_state(db, owned) == before
        assert await db.scalar(select(func.count()).select_from(AdminAuditLog).where(
            AdminAuditLog.action == "project.purge", AdminAuditLog.result == "success")) == 0
        assert await db.scalar(select(func.count()).select_from(AdminAuditLog).where(
            AdminAuditLog.action == "project.purge", AdminAuditLog.result == "failure")) == 1


@pytest.mark.parametrize("mutual_cross_references", [False, True])
async def test_postgresql_parallel_project_purges_do_not_deadlock_or_mutate_other_history(
    postgres_fields_app, postgres_fields_client, monkeypatch, mutual_cross_references,
):
    app, client = postgres_fields_app, postgres_fields_client
    await register(client)
    first = await create_project(client, title="并发作品甲")
    second = await create_project(client, title="并发作品乙")
    a = await _seed_setting_history(app, first["id"], "甲角色")
    b = await _seed_setting_history(app, second["id"], "乙角色")
    if mutual_cross_references:
        async with app.state.sessionmaker.begin() as db:
            for own, other in ((a, b), (b, a)):
                await db.execute(update(SettingTextField).where(SettingTextField.id == own["field"])
                                 .values(definition_id=other["definition"]))
                await db.execute(update(SettingProgression).where(SettingProgression.id == own["event"])
                                 .values(custom_field_id=other["field"]))
    for project in (first, second):
        assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    async with app.state.sessionmaker() as db:
        before = [await _history_state(db, row) for row in (a, b)]
        stamps = {pid: (await db.get(Project, pid)).deleted_at for pid in (first["id"], second["id"])}
    reached = set()
    ready = asyncio.Event()
    original = recycle_bin_service._purge_setting_history

    async def coordinate(db, project_id):
        reached.add(project_id)
        if len(reached) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 10)
        await original(db, project_id)

    monkeypatch.setattr(recycle_bin_service, "_purge_setting_history", coordinate)
    cutoff = project_lifecycle.now_utc() + timedelta(days=31)

    async def purge_one(project_id):
        async with app.state.sessionmaker() as db:
            await recycle_bin_service.purge(db, app.state.task_engine, None, project_id, stamps[project_id],
                                            cutoff=cutoff)

    results = await asyncio.wait_for(asyncio.gather(
        purge_one(first["id"]), purge_one(second["id"]), return_exceptions=True,
    ), 20)
    async with app.state.sessionmaker() as db:
        if mutual_cross_references:
            assert all(isinstance(item, ConflictError) for item in results), results
            assert all([(await db.get(Project, pid)) is not None for pid in stamps])
            assert [await _history_state(db, row) for row in (a, b)] == before
            assert await db.scalar(select(func.count()).select_from(AdminAuditLog).where(
                AdminAuditLog.action == "project.expire", AdminAuditLog.result == "success")) == 0
        else:
            assert results == [None, None]
            assert all([(await db.get(Project, pid)) is None for pid in stamps])
            assert await db.scalar(select(func.count()).select_from(AdminAuditLog).where(
                AdminAuditLog.action == "project.expire", AdminAuditLog.result == "success")) == 2


async def _create_style(app, client):
    user = await register(client)
    async with app.state.sessionmaker.begin() as db:
        style = WritingStyle(user_id=user["id"], preset_key=None, **_style_values())
        db.add(style)
        await db.flush()
        style_id = style.id
    return user, style_id


async def test_postgresql_style_update_waits_for_template_commit(postgres_fields_app, postgres_fields_client):
    app, client = postgres_fields_app, postgres_fields_client
    user, style_id = await _create_style(app, client)
    maker = app.state.sessionmaker
    pending = None

    async def change_style():
        async with maker.begin() as db:
            await db.execute(update(WritingStyle).where(WritingStyle.id == style_id).values(tone="新风格"))

    try:
        async with maker() as creator:
            project = Project(user_id=user["id"], title="并发建书")
            creator.add(project)
            await creator.flush()
            await apply_template(creator, project, _style_snapshot(), {})
            assert project.default_style_id == style_id
            pid = await creator.scalar(text("SELECT pg_backend_pid()"))
            pending = asyncio.create_task(change_style())
            await asyncio.wait_for(_wait_for_blocker(maker, pid), 10)
            assert not pending.done()
            await creator.commit()
            project_id = project.id
        await asyncio.wait_for(pending, 10)
    finally:
        if pending and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
    async with maker() as db:
        project = await db.get(Project, project_id)
        style = await db.get(WritingStyle, style_id)
        # 建书后的风格编辑按现有共享语义生效。
        assert project.default_style_id == style_id and style.tone == "新风格"


async def test_postgresql_style_changed_while_waiting_creates_snapshot_copy(postgres_fields_app,
                                                                     postgres_fields_client):
    app, client = postgres_fields_app, postgres_fields_client
    user, style_id = await _create_style(app, client)
    maker = app.state.sessionmaker
    pending = None

    async def create_from_snapshot():
        async with maker.begin() as db:
            project = Project(user_id=user["id"], title="等待风格修改")
            db.add(project)
            await db.flush()
            await apply_template(db, project, _style_snapshot(), {})
            return project.id, project.default_style_id

    try:
        async with maker() as editor:
            await editor.execute(update(WritingStyle).where(WritingStyle.id == style_id).values(tone="已修改"))
            pid = await editor.scalar(text("SELECT pg_backend_pid()"))
            pending = asyncio.create_task(create_from_snapshot())
            await asyncio.wait_for(_wait_for_blocker(maker, pid), 10)
            assert not pending.done()
            await editor.commit()
        project_id, applied_style_id = await asyncio.wait_for(pending, 10)
    finally:
        if pending and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
    async with maker() as db:
        project = await db.get(Project, project_id)
        applied = await db.scalar(select(WritingStyle).where(WritingStyle.id == applied_style_id))
        assert applied_style_id != style_id and project.default_style_id == applied_style_id
        assert {name: getattr(applied, name) for name in _style_values()} == _style_values()
