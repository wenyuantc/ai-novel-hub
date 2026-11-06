"""分类只读查询：完整匹配分页、字段范围及真实跨连接一致快照。"""

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import create_sessionmaker
from app.core.errors import BadRequestError, ConflictError, NotFoundError, UnauthorizedError
from app.models import (
    Career,
    Chapter,
    Character,
    Organization,
    Project,
    SettingCategory,
    SettingCategoryRule,
    SettingProgression,
    SettingTextField,
    SettingTimeline,
    User,
    WorldSetting,
)
from app.models.setting_fields import SettingFieldDefinition, SettingFieldOption, SettingReferenceHandle
from app.schemas.setting_categories import CategoryEntryQuery, CategorySpec
from app.schemas.setting_fields import EntityRef
from app.services import authorization_service as authz
from app.services import setting_catalog_service as catalog
from app.services import setting_category_query_service as query
from app.services import setting_field_service as fields
from app.services.authorization_service import AuthenticatedActor
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role
from tests.schema_helpers import create_isolated_schema


async def _seed(app, client, *, count=3):
    user = await register(client)
    project = await create_project(client)
    pid = project["id"]
    async with app.state.sessionmaker() as db:
        rows = [
            Character(project_id=pid, name=f"人物{i:03}", sort=i, setting_tags=["同盟"] if i % 2 == 0 else [])
            for i in range(count)
        ]
        org = Organization(project_id=pid, name="同盟组织", setting_tags=["同盟"])
        definition = SettingFieldDefinition(
            project_id=pid, label="阵营", field_type="dropdown", applicable_types=["character", "organization"]
        )
        ignored = SettingFieldDefinition(
            project_id=pid,
            label="无关引用",
            field_type="reference",
            applicable_types=["character"],
            reference_types=["character"],
        )
        category = SettingCategory(
            project_id=pid, name="同行", applicable_types=["character", "organization"], match_mode="all"
        )
        db.add_all([*rows, org, definition, ignored, category])
        await db.flush()
        options = [SettingFieldOption(definition_id=definition.id, label=label) for label in ("旧阵营", "新阵营")]
        db.add_all(options)
        await db.flush()
        for row in rows:
            timeline = SettingTimeline(project_id=pid, subject_kind="character", character_id=row.id, baseline={})
            db.add(timeline)
            await db.flush()
            field = SettingTextField(
                timeline_id=timeline.id,
                definition_id=definition.id,
                label=None,
                base_value={"option_id": options[0].id},
            )
            legacy = SettingTextField(timeline_id=timeline.id, label="不应读取的旧正文", base_value="无关旧资料")
            db.add_all([field, legacy])
            await db.flush()
            db.add(
                SettingProgression(
                    timeline_id=timeline.id,
                    custom_field_id=field.id,
                    operation="replace",
                    value={"option_id": options[1].id},
                    anchor_kind="planned",
                    chapter_no=20,
                    boundary="end",
                )
            )
        rules = [
            SettingCategoryRule(category_id=category.id, sort=0, kind="tag", tag="同盟"),
            SettingCategoryRule(
                category_id=category.id, sort=1, kind="dropdown", definition_id=definition.id, option_id=options[0].id
            ),
        ]
        db.add_all(rules)
        await db.commit()
        actor = AuthenticatedActor(user["id"], (await db.get(User, user["id"])).token_version)
        return SimpleNamespace(
            actor=actor,
            pid=pid,
            characters=[row.id for row in rows],
            org=org.id,
            definition=definition.id,
            ignored=ignored.id,
            options=[row.id for row in options],
            category=category.id,
        )


async def test_scoped_projection_reads_no_legacy_or_handles_and_keeps_default(client, app):
    seeded = await _seed(app, client)
    ref = EntityRef(kind="character", id=seeded.characters[0])
    statements = []

    def capture(_conn, _cursor, statement, params, _context, _many):
        statements.append((statement, params))

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with app.state.sessionmaker() as db:
            scoped = (await fields.resolve_entry_fields(db, seeded.pid, [ref], definition_ids={seeded.definition}))[
                "character", ref.id
            ]
            assert [slot.definition_id for slot in scoped.fields] == [seeded.definition]
            assert scoped.legacy_fields == []
            assert not any("setting_reference_handles" in sql for sql, _ in statements)
            assert all(
                "definition_id" in sql and " IN " in sql for sql, _ in statements if "FROM setting_text_fields" in sql
            )
            statements.clear()
            empty = (await fields.resolve_entry_fields(db, seeded.pid, [ref], definition_ids=set()))[
                "character", ref.id
            ]
            assert empty.fields == [] and empty.legacy_fields == []
            assert not any("setting_progressions" in sql or "setting_text_fields" in sql for sql, _ in statements)
            whole = (await fields.resolve_entry_fields(db, seeded.pid, [ref]))["character", ref.id]
            assert whole.legacy_fields[0].value == "无关旧资料"
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", capture)


async def test_category_uses_old_start_end_values_and_current_tags(client, app):
    seeded = await _seed(app, client)
    for chapter_no, expected in ((5, 2), (20, 2), (21, 0)):
        result = await query.query_category_entries(
            app.state.sessionmaker,
            seeded.actor,
            seeded.pid,
            category_id=seeded.category,
            filters=CategoryEntryQuery(chapter_no=chapter_no),
        )
        assert result.total == expected
        assert result.position.chapter_no == chapter_no
        if result.items:
            assert result.items[0].setting_tags == ["同盟"]
            assert len(result.items[0].reasons) == 2
    current = await query.query_category_entries(
        app.state.sessionmaker, seeded.actor, seeded.pid, category_id=seeded.category
    )
    assert current.total == 0 and current.position is None


async def test_tag_only_complete_matches_are_counted_before_cross_type_pagination(client, app, monkeypatch):
    seeded = await _seed(app, client, count=405)
    draft = CategorySpec(
        name="标签类",
        applicable_types=["character", "organization"],
        match_mode="all",
        rules=[{"kind": "tag", "tag": "同盟"}],
    )
    original = catalog.candidate_batch
    seen = []

    async def batch(*args, **kwargs):
        result = await original(*args, **kwargs)
        seen.append(len(result))
        return result

    async def no_history(*_args, **_kwargs):
        raise AssertionError("纯标签分类不应加载字段历史")

    monkeypatch.setattr(catalog, "candidate_batch", batch)
    monkeypatch.setattr(fields, "resolve_entry_fields", no_history)
    result = await query.query_category_entries(
        app.state.sessionmaker, seeded.actor, seeded.pid, draft=draft, filters=CategoryEntryQuery(page=21, page_size=10)
    )
    assert result.total == 204
    assert [item.reference.kind for item in result.items] == ["character", "character", "character", "organization"]
    assert max(seen) <= 200 and len(seen) >= 3
    empty = await query.query_category_entries(
        app.state.sessionmaker,
        seeded.actor,
        seeded.pid,
        draft=draft,
        filters=CategoryEntryQuery(page=100, page_size=10),
    )
    assert empty.total == 204 and empty.items == []


async def test_scoped_reference_decodes_selected_targets_without_changing_default(client, app):
    seeded = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        timeline = await catalog.find_entry_timeline(db, seeded.pid, "character", seeded.characters[0])
        handles = [
            SettingReferenceHandle(project_id=seeded.pid, kind="character", character_id=cid)
            for cid in seeded.characters
        ]
        db.add_all(handles)
        await db.flush()
        field = SettingTextField(
            timeline_id=timeline.id,
            definition_id=seeded.ignored,
            label=None,
            base_value={"reference_handle_id": handles[0].id},
        )
        db.add(field)
        await db.flush()
        db.add(
            SettingProgression(
                timeline_id=timeline.id,
                custom_field_id=field.id,
                operation="replace",
                value={"reference_handle_id": handles[1].id},
                anchor_kind="planned",
                chapter_no=20,
            )
        )
        await db.commit()
    async with app.state.sessionmaker() as db:
        out = (
            await fields.resolve_entry_fields(
                db, seeded.pid, [EntityRef(kind="character", id=seeded.characters[0])], definition_ids={seeded.ignored}
            )
        )["character", seeded.characters[0]]
        assert len(out.fields) == 1 and out.legacy_fields == []
        assert out.fields[0].base_value == {"kind": "character", "id": seeded.characters[0]}
        assert out.fields[0].value == {"kind": "character", "id": seeded.characters[1]}
        assert out.fields[0].reference.name == "人物001"
        assert out.fields[0].base_reference.name == "人物000"


async def test_retained_archived_rules_require_server_category_identity(client, app):
    seeded = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        stored = await query.load_category(db, seeded.pid, seeded.category)
        await db.execute(
            update(SettingFieldOption).where(SettingFieldOption.id == seeded.options[0]).values(archived=True)
        )
        await db.execute(
            update(SettingFieldDefinition).where(SettingFieldDefinition.id == seeded.definition).values(archived=True)
        )
        await db.commit()
    saved = await query.query_category_entries(
        app.state.sessionmaker,
        seeded.actor,
        seeded.pid,
        category_id=seeded.category,
        filters=CategoryEntryQuery(chapter_no=5),
    )
    assert saved.total == 2
    with pytest.raises(ConflictError, match="停用"):
        await query.query_category_entries(
            app.state.sessionmaker,
            seeded.actor,
            seeded.pid,
            draft=stored.to_spec(),
            filters=CategoryEntryQuery(chapter_no=5),
        )
    editing = await query.query_category_entries(
        app.state.sessionmaker,
        seeded.actor,
        seeded.pid,
        draft=stored.to_spec(),
        editing_category_id=seeded.category,
        filters=CategoryEntryQuery(chapter_no=5),
    )
    assert editing.total == 2
    changed = stored.to_spec()
    changed.rules[1].option_id = seeded.options[1]
    with pytest.raises(ConflictError, match="停用"):
        await query.query_category_entries(
            app.state.sessionmaker, seeded.actor, seeded.pid, draft=changed, editing_category_id=seeded.category
        )
    async with app.state.sessionmaker() as db:
        bad = stored.to_spec()
        bad.rules[1].definition_id = seeded.ignored
        with pytest.raises(BadRequestError):
            await query.validate_category_spec(db, seeded.pid, bad)
        bad.rules[1].definition_id = 999999
        with pytest.raises(NotFoundError):
            await query.validate_category_spec(db, seeded.pid, bad)
        bad.rules[1].definition_id = seeded.definition
        bad.applicable_types = ["world.place"]
        with pytest.raises(BadRequestError, match="共同适用"):
            await query.validate_category_spec(db, seeded.pid, bad, retained_rules=stored.rules)


async def test_null_and_other_field_same_label_do_not_match_and_filters_are_literal(client, app):
    seeded = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        stored = await query.load_category(db, seeded.pid, seeded.category)
        other = SettingFieldDefinition(
            project_id=seeded.pid, label="另一个同组选项", field_type="dropdown", applicable_types=["character"]
        )
        db.add(other)
        await db.flush()
        same_label = SettingFieldOption(definition_id=other.id, label="旧阵营")
        db.add(same_label)
        await db.flush()
        bad = stored.to_spec()
        bad.rules[1].option_id = same_label.id
        with pytest.raises(BadRequestError):
            await query.validate_category_spec(db, seeded.pid, bad)
        await db.execute(
            update(SettingTextField).where(SettingTextField.definition_id == seeded.definition).values(base_value=None)
        )
        await db.commit()
    early = await query.query_category_entries(
        app.state.sessionmaker,
        seeded.actor,
        seeded.pid,
        category_id=seeded.category,
        filters=CategoryEntryQuery(chapter_no=5),
    )
    assert early.total == 0
    draft = CategorySpec(
        name="单标签",
        applicable_types=["character", "organization"],
        match_mode="any",
        rules=[{"kind": "tag", "tag": "同盟"}],
    )
    only_org = await query.query_category_entries(
        app.state.sessionmaker,
        seeded.actor,
        seeded.pid,
        draft=draft,
        filters=CategoryEntryQuery(types=["organization"], q="同盟"),
    )
    assert only_org.total == 1 and only_org.items[0].reference.kind == "organization"
    literal = await query.query_category_entries(
        app.state.sessionmaker, seeded.actor, seeded.pid, draft=draft, filters=CategoryEntryQuery(q="%")
    )
    assert literal.total == 0


async def test_snapshot_rechecks_real_actor_and_project_and_only_issues_reads(client, app):
    seeded = await _seed(app, client)
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await query.query_category_entries(
            app.state.sessionmaker, seeded.actor, seeded.pid, category_id=seeded.category
        )
        assert statements[0] == "BEGIN"
        assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", capture)
    with pytest.raises(UnauthorizedError):
        await query.query_category_entries(
            app.state.sessionmaker,
            AuthenticatedActor(seeded.actor.id, seeded.actor.token_version + 1),
            seeded.pid,
            category_id=seeded.category,
        )
    async with app.state.sessionmaker() as db:
        outsider = User(username="outsider", password_hash="synthetic")
        db.add(outsider)
        await db.commit()
        stranger = AuthenticatedActor(outsider.id, outsider.token_version)
    with pytest.raises(NotFoundError):
        await query.query_category_entries(app.state.sessionmaker, stranger, seeded.pid, category_id=seeded.category)
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.id == seeded.actor.id).values(is_active=False))
        await db.commit()
    with pytest.raises(UnauthorizedError):
        await query.query_category_entries(
            app.state.sessionmaker, seeded.actor, seeded.pid, category_id=seeded.category
        )


async def test_actual_chapter_boundaries_any_and_explicit_clear(client, app):
    seeded = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        category = await query.load_category(db, seeded.pid, seeded.category)
        chapter = Chapter(project_id=seeded.pid, chapter_no=20, title="真实第二十章")
        db.add(chapter)
        await db.flush()
        chapter_id = chapter.id
        timeline = await catalog.find_entry_timeline(db, seeded.pid, "character", seeded.characters[0])
        field_id = await db.scalar(
            select(SettingTextField.id).where(
                SettingTextField.timeline_id == timeline.id,
                SettingTextField.definition_id == seeded.definition,
            )
        )
        db.add(
            SettingProgression(
                timeline_id=timeline.id,
                custom_field_id=field_id,
                operation="replace",
                value=None,
                anchor_kind="planned",
                chapter_no=21,
                boundary="start",
            )
        )
        await db.commit()
    start = await query.query_category_entries(
        app.state.sessionmaker,
        seeded.actor,
        seeded.pid,
        category_id=seeded.category,
        filters=CategoryEntryQuery(chapter_id=chapter_id),
    )
    end = await query.query_category_entries(
        app.state.sessionmaker,
        seeded.actor,
        seeded.pid,
        category_id=seeded.category,
        filters=CategoryEntryQuery(chapter_id=chapter_id, boundary="end"),
    )
    assert start.total == 2 and end.total == 0
    assert start.position.chapter_no == 20 and start.position.chapter_id == chapter_id
    any_spec = category.to_spec().model_copy(update={"match_mode": "any"})
    any_start = await query.query_category_entries(
        app.state.sessionmaker, seeded.actor, seeded.pid, draft=any_spec, filters=CategoryEntryQuery(chapter_no=20)
    )
    any_end = await query.query_category_entries(
        app.state.sessionmaker,
        seeded.actor,
        seeded.pid,
        draft=any_spec,
        filters=CategoryEntryQuery(chapter_no=20, boundary="end"),
    )
    assert any_start.total == 4 and any_end.total == 3
    new_side = category.to_spec()
    new_side.rules[1].option_id = seeded.options[1]
    changed = await query.query_category_entries(
        app.state.sessionmaker,
        seeded.actor,
        seeded.pid,
        draft=new_side,
        filters=CategoryEntryQuery(chapter_no=20, boundary="end"),
    )
    cleared = await query.query_category_entries(
        app.state.sessionmaker, seeded.actor, seeded.pid, draft=new_side, filters=CategoryEntryQuery(chapter_no=21)
    )
    assert changed.total == 2 and cleared.total == 1
    assert cleared.items[0].reference.id == seeded.characters[2]


async def test_internal_kind_sort_order_and_public_directory_order_are_separate(client, app):
    seeded = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        world = WorldSetting(project_id=seeded.pid, title="地点", category="place", setting_tags=["同盟"], sort=-1)
        career = Career(project_id=seeded.pid, name="职业", setting_tags=["同盟"], sort=-10)
        db.add_all([world, career])
        await db.commit()
        candidates, after = [], None
        while batch := await catalog.candidate_batch(db, seeded.pid, after=after, limit=2):
            candidates.extend(batch)
            after = batch[-1].cursor
        assert [item.entry.reference.kind for item in candidates] == ["character"] * 3 + [
            "world",
            "organization",
            "career",
        ]
        public = await catalog.list_entries(db, seeded.actor.id, seeded.pid)
        assert public.items[0].reference.kind == "career"
    spec = CategorySpec(
        name="四实体",
        match_mode="all",
        applicable_types=["character", "world.place", "organization", "career"],
        rules=[{"kind": "tag", "tag": "同盟"}],
    )
    result = await query.query_category_entries(app.state.sessionmaker, seeded.actor, seeded.pid, draft=spec)
    assert result.total == 5
    assert [item.reference.kind for item in result.items] == [
        "character",
        "character",
        "world",
        "organization",
        "career",
    ]
    assert (
        await client.get(f"/api/v1/projects/{seeded.pid}/setting-entries", params={"page_size": 101})
    ).status_code == 422


async def test_dropdown_sql_is_per_batch_and_excludes_unrelated_history(client, app):
    seeded = await _seed(app, client, count=405)
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await query.query_category_entries(
            app.state.sessionmaker,
            seeded.actor,
            seeded.pid,
            category_id=seeded.category,
            filters=CategoryEntryQuery(chapter_no=5),
        )
        assert result.total == 203
        histories = [sql for sql in statements if "FROM setting_progressions" in sql]
        assert len(histories) == 3
        assert all("setting_progressions.custom_field_id IN" in sql for sql in histories)
        assert not any("setting_reference_handles" in sql for sql in statements)
        assert all(
            "setting_text_fields.definition_id IN" in sql for sql in statements if "FROM setting_text_fields" in sql
        )
        assert len(statements) < 100
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", capture)


async def test_helpers_do_not_flush_pending_callers_objects(client, app):
    seeded = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        category = await query.load_category(db, seeded.pid, seeded.category)
        pending = Character(project_id=seeded.pid, name="尚未提交")
        db.add(pending)

        def reject_flush(*_):
            raise AssertionError("分类只读helper触发了未保存对象")

        event.listen(db.sync_session, "before_flush", reject_flush)
        try:
            await query.validate_category_spec(db, seeded.pid, category.to_spec(), retained_rules=category.rules)
            await catalog.candidate_batch(db, seeded.pid)
            await fields.resolve_entry_fields(
                db,
                seeded.pid,
                [EntityRef(kind="character", id=seeded.characters[0])],
                definition_ids={seeded.definition},
            )
            assert pending in db.new and pending.id is None
        finally:
            event.remove(db.sync_session, "before_flush", reject_flush)


@pytest.mark.parametrize("state", ["deleted_at", "deletion_started_at"])
async def test_project_lifecycle_is_rechecked_inside_the_new_snapshot(client, app, state):
    seeded = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        values = {state: datetime.now(UTC)}
        if state == "deletion_started_at":
            values["deletion_token"] = str(uuid4())
        await db.execute(update(Project).where(Project.id == seeded.pid).values(**values))
        await db.commit()
    expected = NotFoundError if state == "deleted_at" else ConflictError
    with pytest.raises(expected):
        await query.query_category_entries(
            app.state.sessionmaker, seeded.actor, seeded.pid, category_id=seeded.category
        )


async def test_rule_metadata_and_editing_retention_never_cross_project(client, app):
    seeded = await _seed(app, client)
    other = await create_project(client, title="另一本书")
    async with app.state.sessionmaker() as db:
        definition = SettingFieldDefinition(
            project_id=other["id"], label="跨作品字段", field_type="dropdown", applicable_types=["character"]
        )
        category = SettingCategory(
            project_id=other["id"], name="跨作品分类", match_mode="all", applicable_types=["character"]
        )
        db.add_all([definition, category])
        await db.flush()
        option = SettingFieldOption(definition_id=definition.id, label="跨作品选项")
        db.add(option)
        await db.flush()
        db.add(
            SettingCategoryRule(
                category_id=category.id, kind="dropdown", definition_id=definition.id, option_id=option.id
            )
        )
        await db.commit()
        foreign_id = category.id
        spec = CategorySpec(
            name="非法跨作品条件",
            applicable_types=["character"],
            match_mode="all",
            rules=[{"kind": "dropdown", "definition_id": definition.id, "option_id": option.id}],
        )
        with pytest.raises(NotFoundError):
            await query.validate_category_spec(db, seeded.pid, spec)
    with pytest.raises(NotFoundError):
        await query.query_category_entries(app.state.sessionmaker, seeded.actor, seeded.pid, category_id=foreign_id)
    with pytest.raises(NotFoundError):
        await query.query_category_entries(
            app.state.sessionmaker, seeded.actor, seeded.pid, draft=spec, editing_category_id=foreign_id
        )


async def test_projects_read_role_can_use_readonly_draft_preview(client, app, system_admin, make_client):
    seeded = await _seed(app, client)
    viewer_client = make_client()
    viewer = await register(viewer_client, "category_viewer")
    role = await create_role(system_admin, "只读分类预览", ["projects.read"])
    await assign_roles(system_admin, viewer["id"], [role["id"]])
    async with app.state.sessionmaker() as db:
        user = await db.get(User, viewer["id"])
        actor = AuthenticatedActor(user.id, user.token_version)
        category = await query.load_category(db, seeded.pid, seeded.category)
    out = await query.query_category_entries(
        app.state.sessionmaker,
        actor,
        seeded.pid,
        draft=category.to_spec(),
        editing_category_id=seeded.category,
        filters=CategoryEntryQuery(chapter_no=5),
    )
    assert out.total == 2


@pytest.fixture(params=["sqlite", "postgresql"])
async def snapshot_database(request, app, client):
    if request.param == "sqlite":
        # 仅本测试隔离库启用WAL，让读快照未结束时另一真实连接可提交。
        async with app.state.engine.connect() as conn:
            assert await conn.scalar(text("PRAGMA journal_mode=WAL")) == "wal"
        seeded = await _seed(app, client, count=405)
        yield app.state.sessionmaker, seeded
        return
    database_url = os.environ.get("AINOVEL_FIELDS_POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("设置 AINOVEL_FIELDS_POSTGRES_TEST_URL 才运行隔离 PostgreSQL 一致快照验证")
    parsed = make_url(database_url)
    if (
        parsed.drivername != "postgresql+asyncpg"
        or parsed.host != "127.0.0.1"
        or parsed.port != 32814
        or parsed.database != "fields_verify"
    ):
        raise AssertionError("分类查询PG验证仅允许本任务的本机fields_verify隔离库")
    schema = f"setting_category_query_test_{uuid4().hex}"
    admin_engine = create_async_engine(database_url)
    engine = create_async_engine(database_url, connect_args={"server_settings": {"search_path": schema}})
    created_schema = False
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        created_schema = True
        async with engine.begin() as conn:
            await conn.run_sync(create_isolated_schema)
        sessionmaker = create_sessionmaker(engine)
        await authz.initialize(sessionmaker)
        async with sessionmaker() as db:
            owner = User(username="pg_author", password_hash="synthetic")
            db.add(owner)
            await db.flush()
            project = Project(user_id=owner.id, title="PG隔离分类快照")
            db.add(project)
            await db.flush()
            characters = [
                Character(
                    project_id=project.id, name=f"人物{i:03}", sort=i, setting_tags=["同盟"] if i % 2 == 0 else []
                )
                for i in range(405)
            ]
            db.add_all(characters)
            definition = SettingFieldDefinition(
                project_id=project.id, label="阵营", field_type="dropdown", applicable_types=["character"]
            )
            category = SettingCategory(
                project_id=project.id, name="同行", applicable_types=["character"], match_mode="all"
            )
            db.add_all([definition, category])
            await db.flush()
            options = [SettingFieldOption(definition_id=definition.id, label=label) for label in ("旧阵营", "新阵营")]
            db.add_all(options)
            await db.flush()
            timelines = [
                SettingTimeline(project_id=project.id, subject_kind="character", character_id=row.id, baseline={})
                for row in characters
            ]
            db.add_all(timelines)
            await db.flush()
            db.add_all(
                [
                    SettingTextField(
                        timeline_id=row.id,
                        definition_id=definition.id,
                        label=None,
                        base_value={"option_id": options[0].id},
                    )
                    for row in timelines
                ]
            )
            db.add_all(
                [
                    SettingCategoryRule(category_id=category.id, sort=0, kind="tag", tag="同盟"),
                    SettingCategoryRule(
                        category_id=category.id,
                        sort=1,
                        kind="dropdown",
                        definition_id=definition.id,
                        option_id=options[0].id,
                    ),
                ]
            )
            await db.commit()
            seeded = SimpleNamespace(
                pid=project.id,
                actor=AuthenticatedActor(owner.id, owner.token_version),
                category=category.id,
                definition=definition.id,
                options=[row.id for row in options],
                characters=[row.id for row in characters],
            )
        yield sessionmaker, seeded
    finally:
        await engine.dispose()
        try:
            if created_schema:
                async with admin_engine.begin() as conn:
                    await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await admin_engine.dispose()


@pytest.mark.parametrize("change", ["tags", "definition", "rules", "field"])
async def test_every_candidate_batch_observes_one_real_snapshot(snapshot_database, monkeypatch, change):
    sessionmaker, seeded = snapshot_database
    reached, changed = asyncio.Event(), asyncio.Event()
    original = catalog.candidate_batch
    paused = False

    async def pause_after_first(*args, **kwargs):
        nonlocal paused
        result = await original(*args, **kwargs)
        if not paused and len(result) == 200:
            paused = True
            reached.set()
            await asyncio.wait_for(changed.wait(), timeout=10)
        return result

    monkeypatch.setattr(catalog, "candidate_batch", pause_after_first)
    reader = asyncio.create_task(
        query.query_category_entries(
            sessionmaker,
            seeded.actor,
            seeded.pid,
            category_id=seeded.category,
            filters=CategoryEntryQuery(chapter_no=5, page_size=100),
        )
    )
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        async with sessionmaker() as writer:
            if change == "tags":
                await writer.execute(
                    update(Character)
                    .where(Character.project_id == seeded.pid, Character.sort >= 200)
                    .values(setting_tags=[])
                )
            elif change == "definition":
                await writer.execute(
                    update(SettingFieldDefinition)
                    .where(SettingFieldDefinition.id == seeded.definition)
                    .values(label="并发新阵营字段")
                )
            elif change == "rules":
                await writer.execute(
                    update(SettingCategoryRule)
                    .where(SettingCategoryRule.category_id == seeded.category, SettingCategoryRule.kind == "tag")
                    .values(tag="不存在的新标签")
                )
            else:
                tids = select(SettingTimeline.id).where(
                    SettingTimeline.project_id == seeded.pid, SettingTimeline.character_id.in_(seeded.characters[200:])
                )
                await writer.execute(
                    update(SettingTextField)
                    .where(SettingTextField.timeline_id.in_(tids), SettingTextField.definition_id == seeded.definition)
                    .values(base_value={"option_id": seeded.options[1]})
                )
                await writer.execute(
                    update(SettingTimeline)
                    .where(SettingTimeline.id.in_(tids))
                    .values(revision=SettingTimeline.revision + 1)
                )
            await writer.execute(
                update(Project)
                .where(Project.id == seeded.pid)
                .values(
                    fields_revision=Project.fields_revision + (1 if change == "definition" else 0),
                    categories_revision=Project.categories_revision + (1 if change in ("tags", "rules") else 0),
                )
            )
            await writer.commit()
        changed.set()
        result = await asyncio.wait_for(reader, timeout=10)
        assert result.total == 203
        assert result.fields_revision == result.categories_revision == 0
        assert result.items[0].reasons[1].definition_label == "阵营"
        fresh = await query.query_category_entries(
            sessionmaker,
            seeded.actor,
            seeded.pid,
            category_id=seeded.category,
            filters=CategoryEntryQuery(chapter_no=5),
        )
        assert fresh.total == {"tags": 100, "definition": 203, "rules": 0, "field": 100}[change]
        if change == "definition":
            assert fresh.items[0].reasons[1].definition_label == "并发新阵营字段"
    finally:
        changed.set()
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
