"""隔离建库 fixture 保留模型身份和收据保护；迁移本身由独立测试验证。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, create_mock_engine, text
from sqlalchemy.exc import IntegrityError

from app.models import AnalysisIndexReceipt, Project, User
from tests.schema_helpers import create_isolated_schema

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


def test_mock_postgresql_schema_keeps_dialect_checks_and_security_ddl():
    statements = []
    engine = create_mock_engine("postgresql://", lambda statement, *_a, **_kw: statements.append(
        str(statement.compile(dialect=engine.dialect)),
    ))
    create_isolated_schema(engine)
    sql = "\n".join(statements)
    assert "CREATE UNIQUE INDEX ux_ai_configs_instance_key" in sql
    assert "ALTER TABLE ai_configs ALTER COLUMN instance_key SET NOT NULL" in sql
    for name in (
        "ai_configs_instance_key_generate_fn", "ai_configs_instance_key_immutable_fn",
        "ai_configs_instance_key_generate BEFORE INSERT", "ai_configs_instance_key_immutable BEFORE UPDATE",
        "analysis_index_receipts_immutable_fn", "analysis_index_receipts_immutable BEFORE UPDATE",
    ):
        assert name in sql
    assert "json_typeof(memory_refs) = 'array'" in sql
    assert "json_valid(memory_refs)" not in sql


def test_sqlite_schema_clone_enforces_generated_immutable_unique_config_identity():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as db:
            create_isolated_schema(db)
            names = set(db.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'")).scalars())
            assert {
                "ai_configs_instance_key_generate", "ai_configs_instance_key_immutable",
                "analysis_index_receipts_immutable",
            } <= names
            user_id = db.scalar(User.__table__.insert().values(
                username="fixture-owner", password_hash="test",
            ).returning(User.id))
            insert = text("""
                INSERT INTO ai_configs (user_id, name, provider, api_key_enc, model, model_type, is_default,
                                        thinking_level)
                VALUES (:owner, '配置', 'openai_compat', 'cipher', 'model', 'text', false, 'off')
            """)
            config_id = db.execute(insert, {"owner": user_id}).lastrowid
            instance_key = db.scalar(text("SELECT instance_key FROM ai_configs WHERE id = :id"), {"id": config_id})
            assert str(UUID(instance_key)) == instance_key and UUID(instance_key).version == 4
            with pytest.raises(IntegrityError):
                with db.begin_nested():
                    db.execute(text("UPDATE ai_configs SET instance_key = :key WHERE id = :id"),
                               {"key": str(uuid4()), "id": config_id})
            with pytest.raises(IntegrityError):
                with db.begin_nested():
                    db.execute(text("""
                        INSERT INTO ai_configs (user_id, name, provider, api_key_enc, model, model_type,
                                                is_default, thinking_level, instance_key)
                        VALUES (:owner, '重复', 'openai_compat', 'cipher', 'model', 'text', false, 'off', :key)
                    """), {"owner": user_id, "key": instance_key})
            assert db.scalar(text("SELECT instance_key FROM ai_configs WHERE id = :id"), {"id": config_id}) \
                == instance_key
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_isolated_postgresql_enforces_config_and_receipt_identity(postgres_collaboration_apps):
    app, _ = postgres_collaboration_apps
    async with app.state.engine.begin() as db:
        schema = await db.scalar(text("SELECT current_schema()"))
        triggers = (await db.execute(text("""
            SELECT cls.relname, trg.tgname, proc.proname, fn_ns.nspname, pg_get_triggerdef(trg.oid)
            FROM pg_trigger AS trg
            JOIN pg_class AS cls ON cls.oid = trg.tgrelid
            JOIN pg_namespace AS tbl_ns ON tbl_ns.oid = cls.relnamespace
            JOIN pg_proc AS proc ON proc.oid = trg.tgfoid
            JOIN pg_namespace AS fn_ns ON fn_ns.oid = proc.pronamespace
            WHERE tbl_ns.nspname = current_schema() AND NOT trg.tgisinternal
              AND cls.relname IN ('ai_configs', 'analysis_index_receipts')
        """))).all()
        by_name = {row.tgname: row for row in triggers}
        assert {
            "ai_configs_instance_key_generate", "ai_configs_instance_key_immutable",
            "analysis_index_receipts_immutable",
        } <= set(by_name)
        assert all(by_name[name].nspname == schema for name in (
            "ai_configs_instance_key_generate", "ai_configs_instance_key_immutable",
            "analysis_index_receipts_immutable",
        ))
        assert "BEFORE INSERT" in by_name["ai_configs_instance_key_generate"].pg_get_triggerdef
        assert "BEFORE UPDATE" in by_name["ai_configs_instance_key_immutable"].pg_get_triggerdef
        assert "BEFORE UPDATE" in by_name["analysis_index_receipts_immutable"].pg_get_triggerdef
        index = await db.scalar(text("""
            SELECT indexdef FROM pg_indexes
            WHERE schemaname = current_schema() AND indexname = 'ux_ai_configs_instance_key'
        """))
        assert index and "UNIQUE INDEX" in index and "instance_key" in index
        assert await db.scalar(text("""
            SELECT attnotnull FROM pg_attribute
            WHERE attrelid = 'ai_configs'::regclass AND attname = 'instance_key' AND NOT attisdropped
        """)) is True
        constraints = (await db.execute(text("""
            SELECT conname, contype::text AS contype, pg_get_constraintdef(oid)
            FROM pg_constraint WHERE conrelid = 'analysis_index_receipts'::regclass
        """))).all()
        by_constraint = {row.conname: row for row in constraints}
        assert by_constraint["uq_analysis_index_receipts_source"].contype == "u"
        assert by_constraint["uq_analysis_index_receipts_child_task_key"].contype == "u"
        check = by_constraint["ck_analysis_index_receipts_memory_refs_array"].pg_get_constraintdef
        assert "json_typeof" in check and "json_valid" not in check

        user_id = await db.scalar(User.__table__.insert().values(
            username=f"schema-{uuid4()}", password_hash="test",
        ).returning(User.id))
        insert_config = text("""
            INSERT INTO ai_configs (user_id, name, provider, api_key_enc, model, model_type, is_default, thinking_level)
            VALUES (:owner, '配置', 'openai_compat', 'cipher', 'model', 'text', false, 'off')
            RETURNING id, instance_key
        """)
        config_id, instance_key = (await db.execute(insert_config, {"owner": user_id})).one()
        assert str(UUID(instance_key)) == instance_key and UUID(instance_key).version == 4
        with pytest.raises(IntegrityError) as duplicate:
            async with db.begin_nested():
                await db.execute(text("""
                    INSERT INTO ai_configs (user_id, name, provider, api_key_enc, model, model_type,
                                            is_default, thinking_level, instance_key)
                    VALUES (:owner, '重复', 'openai_compat', 'cipher', 'model', 'text', false, 'off', :key)
                """), {"owner": user_id, "key": instance_key})
        assert duplicate.value.orig.sqlstate == "23505"
        with pytest.raises(IntegrityError) as replaced:
            async with db.begin_nested():
                await db.execute(text("UPDATE ai_configs SET instance_key = :key WHERE id = :id"),
                                 {"key": str(uuid4()), "id": config_id})
        assert replaced.value.orig.sqlstate == "23514"
        assert await db.scalar(text("SELECT instance_key FROM ai_configs WHERE id = :id"), {"id": config_id}) \
            == instance_key

        project_id = await db.scalar(Project.__table__.insert().values(user_id=user_id, title="收据隔离").returning(
            Project.id,
        ))
        source_key, receipt_key, execution_key = str(uuid4()), str(uuid4()), str(uuid4())
        await db.execute(AnalysisIndexReceipt.__table__.insert().values(
            receipt_key=receipt_key, source_kind="single", source_key=source_key, outcome="no_work",
            source_task_key=source_key, parent_task_key=source_key, parent_execution_key=execution_key,
            parent_queued_at=datetime.now(UTC), actor_id=user_id, actor_token_version=0, project_id=project_id,
            chapter_id=1, analysis_id=1, analysis_hash="a" * 64, content_hash="b" * 64, input_hash="c" * 64,
            selection_hash="d" * 64, memory_refs=[], memory_hash="e" * 64,
        ))
        with pytest.raises(IntegrityError) as immutable:
            async with db.begin_nested():
                await db.execute(text("UPDATE analysis_index_receipts SET outcome = 'no_work' WHERE receipt_key = :k"),
                                 {"k": receipt_key})
        assert immutable.value.orig.sqlstate == "23514"
