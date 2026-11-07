import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

BACKEND_DIR = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {"users", "ai_configs", "projects", "world_settings", "characters", "outlines", "chapters"}


def test_0031_setting_progressions_preserves_legacy_and_detaches_deleted_sources(tmp_path):
    """旧档案成为待校准基线；删源章节不删除剧情事件或既有关系历史。"""
    db_file = tmp_path / "migrate0031.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "0030")

    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'legacy','hash')"))
            conn.execute(text(
                "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                "VALUES (1,1,'旧作品','','','','planning'), (2,1,'其他作品','','','','planning')"
            ))
            conn.execute(text(
                "INSERT INTO characters(id,project_id,name,role_type,profile,relations_text,sort,"
                "appearance,personality,background,current_state,life_status,age,gender) VALUES "
                "(1,1,'林远','主角','少年','',1,'白衣','坚毅','山村','筑基','alive','18','男'),"
                "(2,1,'张三','导师','','',2,'','','','','alive','',''),"
                "(3,2,'旁人','','','',1,'','','','','alive','','')"
            ))
            conn.execute(text(
                "INSERT INTO world_settings(id,project_id,title,content,category,sort) "
                "VALUES (1,1,'青云山','仙山','place',1)"
            ))
            conn.execute(text(
                "INSERT INTO organizations(id,project_id,name,sort) VALUES (1,1,'青云宗',1)"
            ))
            conn.execute(text(
                "INSERT INTO organization_members(id,organization_id,character_id,position,status,note) "
                "VALUES (1,1,1,'内门弟子','active','持令牌')"
            ))
            conn.execute(text(
                "INSERT INTO relationships(id,project_id,a_id,b_id,pair_low,pair_high,type_id,intimacy,"
                "status,description) "
                "VALUES (1,1,2,1,1,2,1,50,'normal','师徒相知')"
            ))
            conn.execute(text(
                "INSERT INTO relationship_history(id,relationship_id,chapter_no,source,changes,note) "
                "VALUES (1,1,20,'analysis',:changes,'必须保留')"
            ), {"changes": '{"status":["past","normal"]}'})
            conn.execute(text("INSERT INTO careers(id,project_id,name,kind,sort) VALUES (1,1,'剑修','main',1)"))
            conn.execute(text("INSERT INTO career_stages(id,career_id,position,name) VALUES (1,1,1,'筑基')"))
            conn.execute(text(
                "INSERT INTO character_careers(id,character_id,career_id,role,stage_id,note) "
                "VALUES (1,1,1,'main',1,'初期')"
            ))
            conn.execute(text(
                "INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                "VALUES (1,1,20,'晋升','正文','draft',2)"
            ))
            conn.execute(text(
                "INSERT INTO chapter_analyses (id,project_id,chapter_id,content_hash,chapter_no,summary,"
                "plot_points,hooks,conflict,emotion,changes_raw,foreshadow_candidates,scores,metrics,"
                "suggestions,warnings) "
                "VALUES (1,1,1,'hash',20,'','[]','[]','{}','{}','{}','[]','{}','{}','[]','[]')"
            ))
            conn.execute(text(
                "INSERT INTO analysis_changes(id,analysis_id,project_id,chapter_no,kind,target,status) "
                "VALUES (1,1,1,20,'character_state','{}','applied')"
            ))
    finally:
        engine.dispose()

    command.upgrade(cfg, "0031")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        inspector = inspect(engine)
        assert {"setting_timelines", "setting_text_fields", "setting_progressions"} <= set(inspector.get_table_names())
        assert "is_present" in {col["name"] for col in inspector.get_columns("relationships")}
        # 0031 保持当时的文本字段合同；0035、0037 的增量由各自迁移测试覆盖。
        from app.models import Base

        for table_name in ("setting_timelines", "setting_text_fields", "setting_progressions"):
            model = Base.metadata.tables[table_name]
            columns = {column["name"]: column for column in inspector.get_columns(table_name)}
            newer_columns = ({"definition_id", "ai_hidden"} if table_name == "setting_text_fields"
                             else {"scene_id"} if table_name == "setting_progressions" else set())
            assert set(columns) == set(model.columns.keys()) - newer_columns
            for column in model.columns:
                if column.name in newer_columns:
                    continue
                nullable = False if table_name == "setting_text_fields" and column.name == "label" else column.nullable
                assert columns[column.name]["nullable"] == nullable
            if table_name == "setting_text_fields":
                assert str(columns["base_value"]["type"]) == "TEXT"
            actual_fks = {
                (tuple(fk["constrained_columns"]), fk["referred_table"], fk["options"].get("ondelete"))
                for fk in inspector.get_foreign_keys(table_name)
            }
            model_fks = {
                (tuple(column.name for column in fk.columns), fk.referred_table.name, fk.ondelete)
                for fk in model.foreign_key_constraints
                if fk.referred_table.name not in {"setting_field_definitions", "scenes"}
            }
            assert actual_fks == model_fks
            assert {index["name"] for index in inspector.get_indexes(table_name)} == {
                index.name for index in model.indexes
                if index.name not in {
                    "uq_setting_timelines_organization", "uq_setting_timelines_career_entry",
                    "ix_setting_text_fields_definition_id", "ix_setting_progressions_scene_id",
                }
            }
        source_fk = next(
            fk for fk in inspector.get_foreign_keys("setting_progressions")
            if fk["constrained_columns"] == ["source_change_id"]
        )
        assert source_fk["options"] == {
            "ondelete": "SET NULL", "deferrable": True, "initially": "DEFERRED"
        }
        model_source_fk = next(
            fk for fk in Base.metadata.tables["setting_progressions"].foreign_key_constraints
            if [col.name for col in fk.columns] == ["source_change_id"]
        )
        assert (model_source_fk.ondelete, model_source_fk.deferrable, model_source_fk.initially) == (
            "SET NULL", True, "DEFERRED"
        )
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            rows = conn.execute(text(
                "SELECT id,project_id,subject_kind,character_id,related_character_id,world_setting_id,career_id,"
                "organization_id,baseline,baseline_status,legacy_compat_allowed FROM setting_timelines"
            )).mappings().all()
            assert len(rows) == 7
            assert all(row["baseline_status"] == "legacy_unverified" and row["legacy_compat_allowed"] for row in rows)
            character = next(row for row in rows if row["subject_kind"] == "character" and row["character_id"] == 1)
            assert json.loads(character["baseline"]) == {
                "profile": "少年", "appearance": "白衣", "personality": "坚毅", "background": "山村",
                "current_state": "筑基", "life_status": "alive", "age": "18", "gender": "男",
            }
            world = next(row for row in rows if row["subject_kind"] == "world")
            assert (world["world_setting_id"], json.loads(world["baseline"])) == (1, {"content": "仙山"})
            relation = next(row for row in rows if row["subject_kind"] == "relationship")
            assert (relation["character_id"], relation["related_character_id"]) == (1, 2)
            assert json.loads(relation["baseline"]) == {
                "exists": True, "identity": {"a_id": 2, "b_id": 1, "type_id": 1},
                "intimacy": 50, "status": "normal", "description": "师徒相知",
            }
            career = next(row for row in rows if row["subject_kind"] == "career")
            assert (career["character_id"], career["career_id"], json.loads(career["baseline"])) == (
                1, 1, {"exists": True, "role": "main", "stage_id": 1, "note": "初期"},
            )
            member = next(row for row in rows if row["subject_kind"] == "membership")
            assert (member["character_id"], member["organization_id"], json.loads(member["baseline"])) == (
                1, 1, {"exists": True, "position": "内门弟子", "status": "active", "note": "持令牌"},
            )
            assert conn.execute(text("SELECT is_present,description FROM relationships WHERE id=1")).one() == (
                1, "师徒相知"
            )
            assert conn.execute(text("SELECT note FROM relationship_history WHERE id=1")).scalar_one() == "必须保留"
            assert conn.execute(text("SELECT count(*) FROM setting_progressions")).scalar_one() == 0
            conn.execute(text(
                "INSERT INTO setting_progressions "
                "(id,timeline_id,field_key,operation,value,anchor_kind,chapter_id,boundary,source,protection,"
                "source_change_id) VALUES (1,:timeline,'current_state','replace',:value,'chapter',1,'end',"
                "'analysis','automatic',1)"
            ), {"timeline": character["id"], "value": json.dumps("金丹", ensure_ascii=False)})
            conn.execute(text("DELETE FROM chapters WHERE id=1"))
            assert conn.execute(text(
                "SELECT anchor_kind,chapter_id,chapter_no,source_change_id,value FROM setting_progressions WHERE id=1"
            )).one() == ("chapter", None, None, None, '"金丹"')
            assert conn.execute(text("SELECT note FROM relationship_history WHERE id=1")).scalar_one() == "必须保留"
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
            conn.execute(text(
                "INSERT INTO setting_text_fields(id,timeline_id,label,base_value) "
                "VALUES (1,:timeline,'地形','青山')"
            ), {"timeline": world["id"]})
            conn.execute(text(
                "INSERT INTO setting_progressions "
                "(id,timeline_id,custom_field_id,operation,value,anchor_kind,chapter_no,boundary) "
                "VALUES (2,:timeline,1,'append',:value,'planned',30,'start')"
            ), {"timeline": world["id"], "value": json.dumps("沙漠", ensure_ascii=False)})
            # 字段与变化同属被删除的主体时，NO ACTION 在语句结束前允许父级的完整级联。
            savepoint = conn.begin_nested()
            conn.execute(text("DELETE FROM projects WHERE id=1"))
            assert conn.execute(text("SELECT count(*) FROM setting_progressions")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM setting_text_fields")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM setting_timelines WHERE project_id=2")).scalar_one() == 1
            savepoint.rollback()

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text(
                "INSERT INTO setting_timelines(project_id,subject_kind,character_id,baseline) "
                "VALUES (1,'character',1,'{}')"
            ))
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("DELETE FROM setting_text_fields WHERE id=1"))
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text(
                "INSERT INTO setting_progressions(timeline_id,operation,value,anchor_kind,chapter_no) "
                "VALUES (:timeline,'replace','\"x\"','planned',5)"
            ), {"timeline": world["id"]})  # 没有内置字段也没有自定义字段，不能产生不明目标变化。
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text(
                "INSERT INTO setting_progressions(timeline_id,field_key,operation,value,anchor_kind,chapter_no) "
                "VALUES (:timeline,'content','replace','\"x\"','chapter',5)"
            ), {"timeline": world["id"]})  # 已关联章节不得同时携带计划章号。
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text(
                "INSERT INTO setting_progressions(timeline_id,field_key,operation,value,anchor_kind,chapter_no,"
                "source_change_id) VALUES (:timeline,'content','replace','\"x\"','planned',5,999999)"
            ), {"timeline": world["id"]})  # 延迟约束仍在提交时拒绝不存在的来源。
    finally:
        engine.dispose()

    # 旧结构没有隐藏载体标记；若直接删列会错误复活关系，且删除载体会丢旧审计。
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE relationships SET is_present=0 WHERE id=1"))
    finally:
        engine.dispose()
    with pytest.raises(RuntimeError, match="已隐藏的人物关系"):
        command.downgrade(cfg, "0030")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        assert "setting_timelines" in inspect(engine).get_table_names()
        with engine.begin() as conn:
            assert conn.execute(text("SELECT note FROM relationship_history WHERE id=1")).scalar_one() == "必须保留"
            conn.execute(text("UPDATE relationships SET is_present=1 WHERE id=1"))
    finally:
        engine.dispose()

    command.downgrade(cfg, "0030")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        assert "setting_timelines" not in inspect(engine).get_table_names()
        assert "is_present" not in {col["name"] for col in inspect(engine).get_columns("relationships")}
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            assert conn.execute(text("SELECT current_state FROM characters WHERE id=1")).scalar_one() == "筑基"
            assert conn.execute(text("SELECT note FROM relationship_history WHERE id=1")).scalar_one() == "必须保留"
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()


def test_0031_rejects_orphan_legacy_subject_before_schema_changes(tmp_path):
    """旧库有孤儿主体时先报错，修复后可重试，不留 SQLite 半完成 DDL。"""
    db_file = tmp_path / "migrate0031-orphan.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "0030")

    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'legacy-orphan','hash')"))
            conn.execute(text(
                "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                "VALUES (1,1,'旧作品','','','','planning')"
            ))
            # 模拟旧库外键当时未开启，错误归属不能被悄悄复制到新时间线。
            conn.execute(text(
                "INSERT INTO characters(id,project_id,name,role_type,profile,relations_text,sort) "
                "VALUES (1,999,'孤儿','','','',1)"
            ))
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="旧角色 #1"):
        command.upgrade(cfg, "0031")

    engine = create_engine(f"sqlite:///{db_file}")
    try:
        assert "setting_timelines" not in inspect(engine).get_table_names()
        assert "is_present" not in {col["name"] for col in inspect(engine).get_columns("relationships")}
        with engine.begin() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0030"
            conn.execute(text("UPDATE characters SET project_id=1 WHERE id=1"))
    finally:
        engine.dispose()

    command.upgrade(cfg, "0031")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT project_id FROM setting_timelines")).scalar_one() == 1
    finally:
        engine.dispose()


def test_0031_postgresql_cascades_sources_and_subjects(monkeypatch):
    """显式提供本地测试库时，在独立 schema 验证真实 PG 双重级联与延迟外键。"""
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext import asyncio as sa_asyncio

    db_url = os.environ.get("AINOVEL_POSTGRES_TEST_URL")
    if not db_url:
        pytest.skip("设置 AINOVEL_POSTGRES_TEST_URL 才运行 PostgreSQL 集成测试")
    parsed = make_url(db_url)
    if parsed.drivername != "postgresql+asyncpg" or parsed.host != "127.0.0.1" or parsed.database != "ainovel_test":
        raise ValueError("PostgreSQL 集成测试仅允许本机 ainovel_test 隔离数据库")

    schema = f"progression_test_{uuid.uuid4().hex}"
    original_factory = sa_asyncio.async_engine_from_config

    def isolated_factory(*args, **kwargs):
        return original_factory(*args, **kwargs, connect_args={"server_settings": {"search_path": schema}})

    async def with_database(action, *, isolated=True):
        options = {"connect_args": {"server_settings": {"search_path": schema}}} if isolated else {}
        engine = sa_asyncio.create_async_engine(db_url, **options)
        try:
            async with engine.begin() as conn:
                database = await conn.scalar(text("SELECT current_database()"))
                assert database == "ainovel_test"
                await action(conn)
        finally:
            await engine.dispose()

    async def create_schema(conn):
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    async def drop_schema(conn):
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    async def seed(conn):
        statements = (
            "INSERT INTO users(id,username,password_hash) VALUES (1,'legacy','hash')",
            "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
            "VALUES (1,1,'旧书','','','','planning'),(2,1,'另一作品','','','','planning')",
            "INSERT INTO characters(id,project_id,name,role_type,profile,relations_text,sort) "
            "VALUES (1,1,'旧角色','','','',1),(2,2,'另一角色','','','',1)",
            "INSERT INTO world_settings(id,project_id,title,content,category,sort) "
            "VALUES (1,1,'旧世界','仙山','place',1)",
            "INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
            "VALUES (1,1,1,'一','','draft',0),(2,1,2,'二','','draft',0)",
            "INSERT INTO chapter_analyses(id,project_id,chapter_id,content_hash,chapter_no,summary,"
            "plot_points,hooks,conflict,emotion,changes_raw,foreshadow_candidates,scores,metrics,suggestions,warnings) "
            "VALUES (1,1,1,'h1',1,'','[]','[]','{}','{}','{}','[]','{}','{}','[]','[]'),"
            "(2,1,2,'h2',2,'','[]','[]','{}','{}','{}','[]','{}','{}','[]','[]')",
            "INSERT INTO analysis_changes(id,analysis_id,project_id,chapter_no,kind,target,status) "
            "VALUES (1,1,1,1,'character_state','{}','applied'),(2,2,1,2,'character_state','{}','applied')",
        )
        for statement in statements:
            await conn.execute(text(statement))

    async def verify(conn):
        options = (await conn.execute(text(
            "SELECT condeferrable,condeferred FROM pg_constraint "
            "WHERE conrelid='setting_progressions'::regclass "
            "AND conname='fk_setting_progressions_source_change_id_analysis_changes'"
        ))).one()
        assert options == (True, True)
        character = await conn.scalar(text(
            "SELECT id FROM setting_timelines WHERE subject_kind='character' AND character_id=1"
        ))
        world = await conn.scalar(text("SELECT id FROM setting_timelines WHERE subject_kind='world'"))
        await conn.execute(text(
            "INSERT INTO setting_progressions(timeline_id,field_key,operation,value,anchor_kind,chapter_id,"
            "source_change_id) VALUES (:timeline,'current_state','replace','\"新状态\"','chapter',1,1)"
        ), {"timeline": character})
        await conn.execute(text("DELETE FROM chapters WHERE id=1"))
        assert (await conn.execute(text(
            "SELECT chapter_id,source_change_id FROM setting_progressions WHERE timeline_id=:timeline"
        ), {"timeline": character})).one() == (None, None)

        field = await conn.scalar(text(
            "INSERT INTO setting_text_fields(timeline_id,label) VALUES (:timeline,'地形') RETURNING id"
        ), {"timeline": world})
        first_custom = await conn.scalar(text(
            "INSERT INTO setting_progressions(timeline_id,custom_field_id,operation,value,anchor_kind,chapter_no) "
            "VALUES (:timeline,:field,'append','\"山脉\"','planned',3) RETURNING id"
        ), {"timeline": world, "field": field})
        replacement = await conn.scalar(text(
            "INSERT INTO setting_progressions(timeline_id,custom_field_id,operation,value,anchor_kind,chapter_no) "
            "VALUES (:timeline,:field,'replace','\"雪原\"','planned',4) RETURNING id"
        ), {"timeline": world, "field": field})
        await conn.execute(text(
            "UPDATE setting_progressions SET active=false,superseded_by_id=:replacement WHERE id=:first"
        ), {"replacement": replacement, "first": first_custom})
        # 另一条事件先提交，随后在不同事务中删章，模拟常见的已保存分析。
        await conn.execute(text(
            "INSERT INTO setting_progressions(timeline_id,field_key,operation,value,anchor_kind,chapter_id,"
            "source_change_id) VALUES (:timeline,'profile','replace','\"已有变化\"','chapter',2,2)"
        ), {"timeline": character})
        async with conn.begin_nested() as savepoint:
            await conn.execute(text("DELETE FROM world_settings WHERE id=1"))
            await conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            assert await conn.scalar(text("SELECT count(*) FROM setting_text_fields")) == 0
            assert await conn.scalar(text("SELECT count(*) FROM setting_progressions WHERE timeline_id=:timeline"),
                                     {"timeline": world}) == 0
            await savepoint.rollback()
        async with conn.begin_nested() as savepoint:
            await conn.execute(text("DELETE FROM projects WHERE id=1"))
            await conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            assert await conn.scalar(text("SELECT count(*) FROM setting_text_fields")) == 0
            assert await conn.scalar(text("SELECT count(*) FROM setting_progressions")) == 0
            assert await conn.scalar(text("SELECT count(*) FROM setting_timelines WHERE project_id=2")) == 1
            await savepoint.rollback()

    async def verify_committed_event(conn):
        await conn.execute(text("DELETE FROM chapters WHERE id=2"))
        assert (await conn.execute(text(
            "SELECT chapter_id,source_change_id FROM setting_progressions WHERE field_key='profile'"
        ))).one() == (None, None)

    async def reject_invalid_source_at_commit(conn):
        timeline = await conn.scalar(text("SELECT id FROM setting_timelines WHERE subject_kind='world'"))
        await conn.execute(text(
            "INSERT INTO setting_progressions(timeline_id,field_key,operation,value,anchor_kind,chapter_no,"
            "source_change_id) VALUES (:timeline,'content','replace','\"x\"','planned',5,999999)"
        ), {"timeline": timeline})

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    asyncio.run(with_database(create_schema, isolated=False))
    try:
        monkeypatch.setattr(sa_asyncio, "async_engine_from_config", isolated_factory)
        command.upgrade(cfg, "0030")
        asyncio.run(with_database(seed))
        command.upgrade(cfg, "0031")
        asyncio.run(with_database(verify))
        asyncio.run(with_database(verify_committed_event))
        with pytest.raises(IntegrityError):
            asyncio.run(with_database(reject_invalid_source_at_commit))
    finally:
        asyncio.run(with_database(drop_schema, isolated=False))


def test_0028_nullable_login_time_preserves_users_and_children(tmp_path):
    """原位修改 users，不回填历史，也不重建父表触发子行 CASCADE/RESTRICT。"""
    db_file = tmp_path / "migrate0028.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "0027")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'historical','preserved-hash')"))
            conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES (2,'empty','other-hash')"))
            conn.execute(text(
                "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                "VALUES (1,1,'原作品','','','','planning')"
            ))
            conn.execute(text(
                "INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                "VALUES (1,1,1,'原章节','保留正文','draft',4)"
            ))
            conn.execute(text(
                "INSERT INTO ai_configs(id,user_id,name,provider,api_key_enc,model,is_default) "
                "VALUES (1,1,'原私有配置','openai_compat','preserved-ciphertext','test',0)"
            ))
    finally:
        engine.dispose()

    command.upgrade(cfg, "0028")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        column = next(col for col in inspect(engine).get_columns("users") if col["name"] == "last_login_at")
        assert column["nullable"] and column["default"] is None
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            assert conn.execute(text("SELECT last_login_at FROM users ORDER BY id")).scalars().all() == [None, None]
            assert conn.execute(text("SELECT password_hash FROM users WHERE id=1")).scalar_one() == "preserved-hash"
            assert conn.execute(text("SELECT content FROM chapters")).scalar_one() == "保留正文"
            assert conn.execute(text("SELECT api_key_enc FROM ai_configs")).scalar_one() == "preserved-ciphertext"
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
            conn.execute(text("UPDATE users SET last_login_at='2026-11-01 01:00:00' WHERE id=1"))
    finally:
        engine.dispose()

    command.downgrade(cfg, "0027")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        assert "last_login_at" not in {col["name"] for col in inspect(engine).get_columns("users")}
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == 2
            assert conn.execute(text("SELECT title FROM projects")).scalar_one() == "原作品"
            assert conn.execute(text("SELECT content FROM chapters")).scalar_one() == "保留正文"
            assert conn.execute(text("SELECT api_key_enc FROM ai_configs")).scalar_one() == "preserved-ciphertext"
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("DELETE FROM users WHERE id=1"))
    finally:
        engine.dispose()


def test_0022_api_key_storage_grants_and_downgrade(tmp_path):
    """凭据升级保留原用户，授权级联不扩大范围，降级只删除新增凭据表。"""
    db_file = tmp_path / "migrate0022.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "0021")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id, username, password_hash) VALUES (1, 'key-owner', 'x')"))
            conn.execute(text("INSERT INTO users (id, username, password_hash) VALUES (2, 'untouched', 'x')"))
            for project_id in (1, 2):
                conn.execute(text(
                    "INSERT INTO projects (id, user_id, title, genre, premise, style, status) "
                    "VALUES (:id, :id, '原有小说', '', '', '', 'planning')"
                ), {"id": project_id})
    finally:
        engine.dispose()

    command.upgrade(cfg, "0022")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        inspection = inspect(engine)
        assert {"api_keys", "api_key_projects"} <= set(inspection.get_table_names())
        indexes = {index["name"]: index for index in inspection.get_indexes("api_keys")}
        assert indexes["ix_api_keys_key_hash"]["unique"]
        grant_fks = inspection.get_foreign_keys("api_key_projects")
        assert {fk["referred_table"] for fk in grant_fks} == {"api_keys", "projects"}
        assert all(fk["options"]["ondelete"] == "CASCADE" for fk in grant_fks)
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text(
                "INSERT INTO api_keys (id, user_id, name, key_hash, key_enc, key_masked) "
                "VALUES (1, 1, '测试密钥', :digest, 'ciphertext', 'anv***1234')"
            ), {"digest": "a" * 64})
            conn.execute(text("INSERT INTO api_key_projects VALUES (1, 1)"))
            assert conn.execute(text("SELECT all_projects FROM api_keys")).scalar_one() == 0
            conn.execute(text("DELETE FROM projects WHERE id=1"))
            assert conn.execute(text("SELECT count(*) FROM api_key_projects")).scalar_one() == 0
            assert conn.execute(text("SELECT all_projects FROM api_keys")).scalar_one() == 0
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO api_keys (user_id, name, key_hash, key_masked) "
                "VALUES (1, '重复摘要', :digest, 'anv***1234')"
            ), {"digest": "a" * 64})
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("DELETE FROM users WHERE id=1"))
            assert conn.execute(text("SELECT count(*) FROM api_keys")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM users WHERE id=2")).scalar_one() == 1
    finally:
        engine.dispose()

    command.downgrade(cfg, "0021")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        assert not ({"api_keys", "api_key_projects"} & set(inspect(engine).get_table_names()))
        with engine.connect() as conn:
            assert conn.execute(text("SELECT title FROM projects WHERE id=2")).scalar_one() == "原有小说"
    finally:
        engine.dispose()


def test_upgrade_and_downgrade(tmp_path):
    db_file = tmp_path / "migrate.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert EXPECTED_TABLES <= set(insp.get_table_names())
        fks = {fk["referred_table"]: fk for fk in insp.get_foreign_keys("chapters")}
        assert fks["projects"]["options"].get("ondelete") == "CASCADE"
        assert fks["outlines"]["options"].get("ondelete") == "SET NULL"
        ai_cols = {c["name"] for c in insp.get_columns("ai_configs")}
        assert {"thinking_level", "context_window_k", "max_output_k", "model_type"} <= ai_cols
        user_cols = {c["name"] for c in insp.get_columns("users")}
        assert {"is_admin", "is_active", "token_version"} <= user_cols
    finally:
        engine.dispose()

    command.downgrade(cfg, "base")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        assert not (EXPECTED_TABLES & set(inspect(engine).get_table_names()))
    finally:
        engine.dispose()


def test_0021_project_cover_storage_and_cascade(tmp_path):
    db_file = tmp_path / "migrate0021.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "0020")
    command.upgrade(cfg, "0021")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert insp.get_pk_constraint("project_covers")["constrained_columns"] == ["project_id"]
        assert insp.get_foreign_keys("project_covers")[0]["options"]["ondelete"] == "CASCADE"
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("INSERT INTO users (id, username, password_hash) VALUES (1, 'cover-user', 'x')"))
            conn.execute(text(
                "INSERT INTO projects (id, user_id, title, genre, premise, style, status) "
                "VALUES (1, 1, '封面项目', '', '', '', 'planning')"
            ))
            conn.execute(text(
                "INSERT INTO project_covers (project_id, mime_type, data) VALUES (1, 'image/png', :data)"
            ), {"data": b"image bytes"})
            assert conn.execute(text("SELECT data FROM project_covers")).scalar_one() == b"image bytes"
            conn.execute(text("DELETE FROM projects WHERE id=1"))
            assert conn.execute(text("SELECT count(*) FROM project_covers")).scalar_one() == 0
    finally:
        engine.dispose()
    command.downgrade(cfg, "0020")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        assert "project_covers" not in inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == 1
    finally:
        engine.dispose()


def test_0003_backfills_existing_users(tmp_path):
    """0003 为存量用户回填：启用中的普通用户、令牌版本 0；downgrade -1 删除三列。"""
    db_file = tmp_path / "migrate0003.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "0002")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
    finally:
        engine.dispose()

    command.upgrade(cfg, "0003")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT is_admin, is_active, token_version FROM users")).one()
        assert tuple(row) == (0, 1, 0)
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("users")}
        assert not ({"is_admin", "is_active", "token_version"} & cols)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT username FROM users")).scalar_one() == "old"
    finally:
        engine.dispose()


def test_0004_backfills_model_type(tmp_path):
    """0004 将存量配置回填为文本模型；downgrade -1 删除该列。"""
    db_file = tmp_path / "migrate0004.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "0003")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO ai_configs (user_id, name, provider, api_key_enc, model, is_default) "
                    "VALUES (1, 'c', 'openai_compat', 'enc', 'm', 1)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0004")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT model_type FROM ai_configs")).scalar_one() == "text"
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("ai_configs")}
        assert "model_type" not in cols
    finally:
        engine.dispose()


def test_0005_chapter_versions(tmp_path):
    """0005 新建 chapter_versions / generation_logs（外键规则正确）；downgrade -1 删除两表且不影响章节。"""
    db_file = tmp_path / "migrate0005.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "0005")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert {"chapter_versions", "generation_logs"} <= set(insp.get_table_names())
        vfks = {fk["referred_table"]: fk["options"].get("ondelete") for fk in insp.get_foreign_keys("chapter_versions")}
        assert vfks == {"chapters": "CASCADE", "generation_logs": "SET NULL"}
        lfks = {fk["referred_table"]: fk["options"].get("ondelete") for fk in insp.get_foreign_keys("generation_logs")}
        assert lfks == {"projects": "CASCADE", "chapters": "SET NULL", "ai_configs": "SET NULL"}
        uqs = [u["column_names"] for u in insp.get_unique_constraints("chapter_versions")]
        assert ["chapter_id", "seq"] in uqs
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert not ({"chapter_versions", "generation_logs"} & tables)
        assert "chapters" in tables
    finally:
        engine.dispose()


def test_0006_tasks(tmp_path):
    """0006 新建 tasks（外键规则、部分唯一索引）并给 generation_logs 加 task_id；downgrade -1 还原。"""
    db_file = tmp_path / "migrate0006.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "0005")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO projects (user_id, title, genre, premise, style, status) "
                    "VALUES (1, 'p', '', '', '', 'planning')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO generation_logs (project_id, kind, provider, model, params, messages, status, "
                    "error, output_chars, duration_ms) VALUES (1, 'generate', '', '', '{}', '[]', 'success', '', 0, 0)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0006")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert "tasks" in insp.get_table_names()

        def _fk_rules(table: str) -> dict:
            return {fk["constrained_columns"][0]: fk["options"].get("ondelete") for fk in insp.get_foreign_keys(table)}

        assert _fk_rules("tasks") == {
            "user_id": "CASCADE",
            "project_id": "CASCADE",
            "chapter_id": "SET NULL",
            "retry_of_id": "SET NULL",
        }
        assert _fk_rules("generation_logs")["task_id"] == "SET NULL"
        idx = {i["name"]: i for i in insp.get_indexes("tasks")}
        assert idx["uq_tasks_active_resource"]["unique"]
        assert idx["ix_tasks_status_id"]["column_names"] == ["status", "id"]
        with engine.connect() as conn:
            # 存量生成记录保留，task_id 为空
            assert conn.execute(text("SELECT task_id FROM generation_logs")).scalar_one() is None

        insert = (
            "INSERT INTO tasks (user_id, kind, resource_key, status, input) "
            "VALUES (1, 'wizard.world', :key, :status, '{}')"
        )
        with engine.begin() as conn:
            conn.execute(text(insert), {"key": "wizard:1:world", "status": "succeeded"})
            conn.execute(text(insert), {"key": "wizard:1:world", "status": "failed"})
            conn.execute(text(insert), {"key": "wizard:1:world", "status": "queued"})
            conn.execute(text(insert), {"key": None, "status": "running"})
            conn.execute(text(insert), {"key": None, "status": "running"})
        # 同资源第二个活动任务被部分唯一索引拒绝
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text(insert), {"key": "wizard:1:world", "status": "running"})
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert "tasks" not in insp.get_table_names()
        assert "task_id" not in {c["name"] for c in insp.get_columns("generation_logs")}
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM generation_logs")).scalar_one() == 1
    finally:
        engine.dispose()


def test_0007_story_entities(tmp_path):
    """0007：存量角色 / 世界观原样保留并回填，写入预置关系类型；downgrade -1 删除新表新列且不影响存量数据。"""
    from app.models.entities import PRESET_RELATIONSHIP_TYPES

    db_file = tmp_path / "migrate0007.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "0006")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO projects (user_id, title, genre, premise, style, status) "
                    "VALUES (1, 'p', '', '', '', 'planning')"
                )
            )
            insert_char = (
                "INSERT INTO characters (project_id, name, role_type, profile, relations_text, sort) "
                "VALUES (1, :name, :role, :profile, :rel, :sort)"
            )
            conn.execute(
                text(insert_char), {"name": "林远", "role": "主角", "profile": "少年", "rel": "师父是张三", "sort": 1}
            )
            conn.execute(text(insert_char), {"name": "Lin", "role": "Protagonist", "profile": "", "rel": "", "sort": 2})
            conn.execute(text(insert_char), {"name": "张三", "role": "导师", "profile": "老者", "rel": "", "sort": 3})
            conn.execute(
                text("INSERT INTO world_settings (project_id, title, content, sort) VALUES (1, '青云山', '仙山', 1)")
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0007")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        assert {
            "organizations",
            "organization_members",
            "relationship_types",
            "relationships",
            "relationship_history",
            "chapter_characters",
        } <= tables

        def _fk_rules(table: str) -> dict:
            return {fk["constrained_columns"][0]: fk["options"].get("ondelete") for fk in insp.get_foreign_keys(table)}

        assert _fk_rules("relationships") == {
            "project_id": "CASCADE",
            "a_id": "CASCADE",
            "b_id": "CASCADE",
            "type_id": None,
        }
        assert _fk_rules("organizations") == {"project_id": "CASCADE", "parent_id": "SET NULL"}
        assert _fk_rules("organization_members") == {"organization_id": "CASCADE", "character_id": "CASCADE"}
        assert _fk_rules("relationship_history") == {"relationship_id": "CASCADE"}
        assert _fk_rules("chapter_characters") == {"chapter_id": "CASCADE", "character_id": "CASCADE"}
        uqs = [u["column_names"] for u in insp.get_unique_constraints("relationships")]
        assert ["pair_low", "pair_high"] in uqs
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT name, profile, relations_text, always_in_context, aliases, life_status "
                    "FROM characters ORDER BY sort"
                )
            ).all()
            # 简介与旧关系备注原样保留；主角回填始终注入
            assert [tuple(r) for r in rows] == [
                ("林远", "少年", "师父是张三", 1, "[]", "alive"),
                ("Lin", "", "", 1, "[]", "alive"),
                ("张三", "老者", "", 0, "[]", "alive"),
            ]
            assert conn.execute(text("SELECT category, content FROM world_settings")).one() == ("other", "仙山")
            presets = conn.execute(
                text("SELECT category, role_a, role_b FROM relationship_types WHERE project_id IS NULL ORDER BY sort")
            ).all()
            assert [tuple(p) for p in presets] == list(PRESET_RELATIONSHIP_TYPES)
            assert len(presets) == 20
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert "relationships" not in insp.get_table_names()
        assert "organizations" not in insp.get_table_names()
        assert "aliases" not in {c["name"] for c in insp.get_columns("characters")}
        assert "category" not in {c["name"] for c in insp.get_columns("world_settings")}
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT relations_text FROM characters WHERE name = '林远'")).scalar_one()
                == "师父是张三"
            )
    finally:
        engine.dispose()


def test_0008_careers(tmp_path):
    """0008：新建职业四张表（外键规则、唯一约束），不改动存量数据；downgrade -1 删除四张表。"""
    db_file = tmp_path / "migrate0008.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "0007")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO projects (user_id, title, genre, premise, style, status) "
                    "VALUES (1, 'p', '', '', '', 'planning')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO characters (project_id, name, role_type, profile, relations_text, sort) "
                    "VALUES (1, '林远', '主角', '少年', '', 1)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0008")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert {"careers", "career_stages", "character_careers", "career_history"} <= set(insp.get_table_names())

        def _fk_rules(table: str) -> dict:
            return {fk["constrained_columns"][0]: fk["options"].get("ondelete") for fk in insp.get_foreign_keys(table)}

        assert _fk_rules("careers") == {"project_id": "CASCADE"}
        assert _fk_rules("career_stages") == {"career_id": "CASCADE"}
        assert _fk_rules("character_careers") == {"character_id": "CASCADE", "career_id": "CASCADE", "stage_id": None}
        assert _fk_rules("career_history") == {"character_id": "CASCADE", "career_id": "CASCADE"}
        assert ["project_id", "name"] in [u["column_names"] for u in insp.get_unique_constraints("careers")]
        assert ["character_id", "career_id"] in [
            u["column_names"] for u in insp.get_unique_constraints("character_careers")
        ]
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("INSERT INTO careers (project_id, name, sort) VALUES (1, '剑修', 1)"))
            conn.execute(text("INSERT INTO career_stages (career_id, position, name) VALUES (1, 1, '练气')"))
            conn.execute(
                text("INSERT INTO character_careers (character_id, career_id, role, stage_id) VALUES (1, 1, 'main', 1)")
            )
            conn.execute(
                text(
                    "INSERT INTO career_history (character_id, career_id, career_name, source, changes) "
                    "VALUES (1, 1, '剑修', 'manual', '{}')"
                )
            )
            # 删除职业：阶段、挂接、历史在同一语句内级联删除（stage_id 不设 ondelete 也不会阻塞）
            conn.execute(text("DELETE FROM careers WHERE id = 1"))
            for table in ("career_stages", "character_careers", "career_history"):
                assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
            assert conn.execute(text("SELECT profile FROM characters WHERE name = '林远'")).scalar_one() == "少年"
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert not ({"careers", "career_stages", "character_careers", "career_history"} & tables)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM characters")).scalar_one() == 1
    finally:
        engine.dispose()


def test_0009_chapter_analysis(tmp_path):
    """0009：分析两表（外键规则）、auto_analyze 回填 true、tasks.serial_key；downgrade -1 删除且存量保留。"""
    db_file = tmp_path / "migrate0009.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "0008")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO projects (user_id, title, genre, premise, style, status) "
                    "VALUES (1, 'p', '', '', '', 'planning')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO chapters (project_id, chapter_no, title, content, status, word_count) "
                    "VALUES (1, 1, '第1章', '正文', 'draft', 2)"
                )
            )
            conn.execute(
                text("INSERT INTO tasks (user_id, kind, status, input) VALUES (1, 'wizard.world', 'succeeded', '{}')")
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0009")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert {"chapter_analyses", "analysis_changes"} <= set(insp.get_table_names())
        assert "serial_key" in {c["name"] for c in insp.get_columns("tasks")}
        assert "ix_tasks_serial_key" in {i["name"] for i in insp.get_indexes("tasks")}

        def _fk_rules(table: str) -> dict:
            return {fk["constrained_columns"][0]: fk["options"].get("ondelete") for fk in insp.get_foreign_keys(table)}

        assert _fk_rules("chapter_analyses") == {
            "project_id": "CASCADE",
            "chapter_id": "CASCADE",
            "task_id": "SET NULL",
        }
        assert _fk_rules("analysis_changes") == {"analysis_id": "CASCADE", "project_id": "CASCADE"}
        with engine.begin() as conn:
            assert conn.execute(text("SELECT auto_analyze FROM projects")).scalar_one() == 1
            assert conn.execute(text("SELECT serial_key FROM tasks")).scalar_one() is None
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(
                text(
                    "INSERT INTO chapter_analyses (project_id, chapter_id, task_id, content_hash, chapter_no, summary, "
                    "plot_points, hooks, conflict, emotion, changes_raw, foreshadow_candidates, scores, metrics, "
                    "suggestions, warnings) VALUES (1, 1, 1, 'h', 1, 's', '[]', '[]', '{}', '{}', '{}', '[]', '{}', "
                    "'{}', '[]', '[]')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO analysis_changes (analysis_id, project_id, chapter_no, kind, target, status) "
                    "VALUES (1, 1, 1, 'character_state', '{}', 'applied')"
                )
            )
            conn.execute(text("DELETE FROM tasks"))
            assert conn.execute(text("SELECT task_id FROM chapter_analyses")).scalar_one() is None
            # 删除章节：分析与回写条目级联删除
            conn.execute(text("DELETE FROM chapters"))
            for table in ("chapter_analyses", "analysis_changes"):
                assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert not ({"chapter_analyses", "analysis_changes"} & set(insp.get_table_names()))
        assert "auto_analyze" not in {c["name"] for c in insp.get_columns("projects")}
        assert "serial_key" not in {c["name"] for c in insp.get_columns("tasks")}
        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM projects")).scalar_one() == 1
    finally:
        engine.dispose()


def test_0010_foreshadows(tmp_path):
    """0010：伏笔三表（外键规则、复合主键）；删角色只解除关联，删伏笔级联历史；downgrade -1 删除三表且存量保留。"""
    db_file = tmp_path / "migrate0010.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "0009")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO projects (user_id, title, genre, premise, style, status) "
                    "VALUES (1, 'p', '', '', '', 'planning')"
                )
            )
            for name in ("甲", "乙"):
                conn.execute(
                    text(
                        "INSERT INTO characters (project_id, name, role_type, profile, relations_text, sort) "
                        f"VALUES (1, '{name}', '', '', '', 0)"
                    )
                )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0010")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert {"foreshadows", "foreshadow_characters", "foreshadow_history"} <= set(insp.get_table_names())

        def _fk_rules(table: str) -> dict:
            return {fk["constrained_columns"][0]: fk["options"].get("ondelete") for fk in insp.get_foreign_keys(table)}

        assert _fk_rules("foreshadows") == {"project_id": "CASCADE"}
        assert _fk_rules("foreshadow_characters") == {"foreshadow_id": "CASCADE", "character_id": "CASCADE"}
        assert _fk_rules("foreshadow_history") == {"foreshadow_id": "CASCADE"}
        assert insp.get_pk_constraint("foreshadow_characters")["constrained_columns"] == [
            "foreshadow_id",
            "character_id",
        ]
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("INSERT INTO foreshadows (project_id, title) VALUES (1, '玉佩')"))
            row = conn.execute(
                text("SELECT status, importance, include_in_context, source, planned_resolve_chapter FROM foreshadows")
            ).one()
            assert tuple(row) == ("pending", 3, 1, "manual", None)
            conn.execute(text("INSERT INTO foreshadow_characters (foreshadow_id, character_id) VALUES (1, 1), (1, 2)"))
            with pytest.raises(IntegrityError):
                with conn.begin_nested():
                    conn.execute(text("INSERT INTO foreshadow_characters (foreshadow_id, character_id) VALUES (1, 1)"))
            conn.execute(
                text("INSERT INTO foreshadow_history (foreshadow_id, source, changes) VALUES (1, 'manual', '{}')")
            )
            # 删除角色：只解除关联，伏笔保留
            conn.execute(text("DELETE FROM characters WHERE id = 1"))
            assert conn.execute(text("SELECT character_id FROM foreshadow_characters")).scalars().all() == [2]
            assert conn.execute(text("SELECT COUNT(*) FROM foreshadows")).scalar_one() == 1
            # 删除伏笔：关联与历史级联删除
            conn.execute(text("DELETE FROM foreshadows"))
            for table in ("foreshadow_characters", "foreshadow_history"):
                assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert not ({"foreshadows", "foreshadow_characters", "foreshadow_history"} & set(insp.get_table_names()))
        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM characters")).scalar_one() == 1
    finally:
        engine.dispose()


def test_0011_outline_pro(tmp_path):
    """0011：细纲 / 快照两表（外键规则、chapter_id 唯一）、chapters.plan_id、projects.wizard_progress 回填 {}；
    删大纲级联细纲且章节保留、plan_id 置空；downgrade -1 删除两表两列且存量保留。"""
    db_file = tmp_path / "migrate0011.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "0010")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO projects (user_id, title, genre, premise, style, status) "
                    "VALUES (1, 'p', '', '', '', 'planning')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO outlines (project_id, volume_no, volume_title, chapter_no, title, summary, sort) "
                    "VALUES (1, 1, '', 1, '纲', '', 1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO chapters (project_id, outline_id, chapter_no, title, content, status, word_count) "
                    "VALUES (1, 1, 1, '一', '正文', 'draft', 2)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0011")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert {"chapter_plans", "outline_snapshots"} <= set(insp.get_table_names())

        def _fk_rules(table: str) -> dict:
            return {fk["constrained_columns"][0]: fk["options"].get("ondelete") for fk in insp.get_foreign_keys(table)}

        assert _fk_rules("chapter_plans") == {
            "project_id": "CASCADE",
            "outline_id": "CASCADE",
            "chapter_id": "SET NULL",
        }
        assert _fk_rules("outline_snapshots") == {"project_id": "CASCADE"}
        assert _fk_rules("chapters")["plan_id"] == "SET NULL"
        assert _fk_rules("chapters")["outline_id"] == "SET NULL"
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            assert conn.execute(text("SELECT wizard_progress FROM projects")).scalar_one() == "{}"
            conn.execute(
                text(
                    "INSERT INTO chapter_plans (project_id, outline_id, seq, title, chapter_id) "
                    "VALUES (1, 1, 1, '细', 1)"
                )
            )
            row = conn.execute(text("SELECT key_events, character_ids, est_words, emotion FROM chapter_plans")).one()
            assert tuple(row) == ("[]", "[]", 3000, "")
            conn.execute(text("UPDATE chapters SET plan_id = 1"))
            with pytest.raises(IntegrityError):
                with conn.begin_nested():
                    conn.execute(
                        text(
                            "INSERT INTO chapter_plans (project_id, outline_id, seq, title, chapter_id) "
                            "VALUES (1, 1, 2, '细2', 1)"
                        )
                    )
            conn.execute(text("INSERT INTO outline_snapshots (project_id, reason, data) VALUES (1, 'manual', '{}')"))
            # 删除大纲：细纲级联删除，章节保留且解除关联
            conn.execute(text("DELETE FROM outlines"))
            assert conn.execute(text("SELECT COUNT(*) FROM chapter_plans")).scalar_one() == 0
            assert tuple(conn.execute(text("SELECT outline_id, plan_id, content FROM chapters")).one()) == (
                None,
                None,
                "正文",
            )
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert not ({"chapter_plans", "outline_snapshots"} & set(insp.get_table_names()))
        assert "plan_id" not in {c["name"] for c in insp.get_columns("chapters")}
        assert "wizard_progress" not in {c["name"] for c in insp.get_columns("projects")}
        with engine.connect() as conn:
            assert conn.execute(text("SELECT content FROM chapters")).scalar_one() == "正文"
    finally:
        engine.dispose()


def test_0012_batch_generate(tmp_path):
    """0012：tasks.checkpoint 可空 JSON（存量任务为 NULL）；downgrade -1 先把 paused 改为 cancelled 再删列。"""
    db_file = tmp_path / "migrate0012.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "0011")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO tasks (user_id, kind, status, input) VALUES (1, 'chapter.generate', 'succeeded', '{}')"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0012")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        cols = {c["name"]: c for c in inspect(engine).get_columns("tasks")}
        assert cols["checkpoint"]["nullable"] is True
        with engine.begin() as conn:
            assert conn.execute(text("SELECT checkpoint FROM tasks")).scalar_one() is None
            conn.execute(
                text(
                    "INSERT INTO tasks (user_id, kind, status, input, checkpoint, resource_key) "
                    "VALUES (1, 'chapter.batch', 'paused', '{}', '{\"phase\": \"generate\"}', 'batch:1')"
                )
            )
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        assert "checkpoint" not in {c["name"] for c in inspect(engine).get_columns("tasks")}
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT kind, status, finished_at FROM tasks ORDER BY id")).all()
            assert [(r[0], r[1]) for r in rows] == [("chapter.generate", "succeeded"), ("chapter.batch", "cancelled")]
            assert rows[1][2] is not None
    finally:
        engine.dispose()


def test_0013_prompt_styles(tmp_path):
    """0013：插入 6 个预设；非空 projects.style 迁移为「《书名》原风格」并设为项目默认；
    downgrade -1 把默认风格写回 style（原文无损），删除新表新列。"""
    import importlib.util

    from app.models.style_presets import PRESET_STYLES

    db_file = tmp_path / "migrate0013.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    long_style = "冷峻克制，" + "长" * 4000 + "\n第二段"
    command.upgrade(cfg, "0012")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
            for title, style in (("星海", long_style), ("空白", "   "), ("短", "凌厉")):
                conn.execute(
                    text(
                        "INSERT INTO projects (user_id, title, genre, premise, style, status) "
                        "VALUES (1, :t, '', '', :s, 'planning')"
                    ),
                    {"t": title, "s": style},
                )
            conn.execute(
                text(
                    "INSERT INTO chapters (project_id, chapter_no, title, content, status, word_count) "
                    "VALUES (1, 1, '', '', 'draft', 0)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0013")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert {"writing_styles", "prompt_overrides", "prompt_override_versions"} <= set(insp.get_table_names())
        fks = {fk["constrained_columns"][0]: fk for fk in insp.get_foreign_keys("projects")}
        assert fks["default_style_id"]["options"].get("ondelete") == "SET NULL"
        fks = {fk["constrained_columns"][0]: fk for fk in insp.get_foreign_keys("chapters")}
        assert fks["style_id"]["options"].get("ondelete") == "SET NULL"
        fks = {fk["constrained_columns"][0]: fk for fk in insp.get_foreign_keys("prompt_override_versions")}
        assert fks["override_id"]["options"].get("ondelete") == "CASCADE"
        with engine.begin() as conn:
            presets = conn.execute(
                text("SELECT preset_key, name FROM writing_styles WHERE user_id IS NULL ORDER BY sort")
            ).all()
            assert [tuple(r) for r in presets] == [(p["preset_key"], p["name"]) for p in PRESET_STYLES]
            rows = conn.execute(
                text(
                    "SELECT p.title, s.user_id, s.name, s.description, s.extra FROM projects p "
                    "LEFT JOIN writing_styles s ON s.id = p.default_style_id ORDER BY p.id"
                )
            ).all()
            assert rows[0][1:] == (1, "《星海》原风格", long_style[:100], long_style)
            assert rows[1][1:] == (None, None, None, None)
            assert rows[2][1:] == (1, "《短》原风格", "凌厉", "凌厉")
            assert conn.execute(text("SELECT style_id FROM chapters")).scalar_one() is None
            # 用户覆盖 (user_id, key) 唯一
            conn.execute(text("INSERT INTO prompt_overrides (user_id, key) VALUES (1, 'chapter.generate')"))
            with pytest.raises(IntegrityError):
                with conn.begin_nested():
                    conn.execute(text("INSERT INTO prompt_overrides (user_id, key) VALUES (1, 'chapter.generate')"))
    finally:
        engine.dispose()

    # 迁移内冻结的预设与应用代码一致
    spec = importlib.util.spec_from_file_location("m0013", BACKEND_DIR / "alembic/versions/0013_prompt_styles.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert tuple(module.PRESET_STYLES) == PRESET_STYLES

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert not ({"writing_styles", "prompt_overrides", "prompt_override_versions"} & set(insp.get_table_names()))
        assert "default_style_id" not in {c["name"] for c in insp.get_columns("projects")}
        assert "style_id" not in {c["name"] for c in insp.get_columns("chapters")}
        with engine.connect() as conn:
            styles = [r[0] for r in conn.execute(text("SELECT style FROM projects ORDER BY id")).all()]
        assert styles == [long_style, "", "凌厉"]
    finally:
        engine.dispose()


def test_0014_inspiration(tmp_path):
    """0014：projects 新增 6 列（存量项目取默认值）、新建 inspiration_sessions（外键规则）；
    downgrade -1 删除新表新列且存量项目保留。"""
    db_file = tmp_path / "migrate0014.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "0013")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO projects (user_id, title, genre, premise, style, status) "
                    "VALUES (1, '星海', '科幻', '设定', '', 'planning')"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0014")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("projects")}
        assert {"synopsis", "theme", "tags", "selling_points", "audience", "target_words"} <= cols
        assert "inspiration_sessions" in insp.get_table_names()
        fks = {fk["constrained_columns"][0]: fk for fk in insp.get_foreign_keys("inspiration_sessions")}
        assert fks["user_id"]["options"].get("ondelete") == "CASCADE"
        assert fks["project_id"]["options"].get("ondelete") == "SET NULL"
        assert fks["converted_project_id"]["options"].get("ondelete") == "SET NULL"
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            row = conn.execute(
                text("SELECT synopsis, theme, tags, selling_points, audience, target_words FROM projects")
            ).one()
            assert tuple(row) == ("", "", "[]", "", "", None)
            conn.execute(
                text(
                    "INSERT INTO inspiration_sessions (user_id, project_id, converted_project_id, title) "
                    "VALUES (1, 1, 1, '会话')"
                )
            )
            status, state = conn.execute(text("SELECT status, state FROM inspiration_sessions")).one()
            assert (status, state) == ("active", "{}")
            # 删除项目：会话保留，两个项目引用置空
            conn.execute(text("DELETE FROM projects"))
            assert tuple(
                conn.execute(text("SELECT project_id, converted_project_id FROM inspiration_sessions")).one()
            ) == (None, None)
            conn.execute(
                text(
                    "INSERT INTO projects (user_id, title, genre, premise, style, status) "
                    "VALUES (1, '留存', '', '', '', 'planning')"
                )
            )
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert "inspiration_sessions" not in insp.get_table_names()
        cols = {c["name"] for c in insp.get_columns("projects")}
        assert not ({"synopsis", "theme", "tags", "selling_points", "audience", "target_words"} & cols)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT title FROM projects")).scalar_one() == "留存"
    finally:
        engine.dispose()


def test_0015_knowledge_base(tmp_path):
    """0015：新建知识库 / 记忆 / 检索单元五张表（外键规则、唯一约束、删章节只解除手动记忆关联）；
    downgrade -1 删除五张表且存量项目保留。"""
    db_file = tmp_path / "migrate0015.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "0014")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO projects (user_id, title, genre, premise, style, status) "
                    "VALUES (1, '星海', '科幻', '设定', '', 'planning')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO chapters (project_id, chapter_no, title, content, status, word_count) "
                    "VALUES (1, 1, '一', '正文', 'draft', 2)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0015")
    engine = create_engine(f"sqlite:///{db_file}")
    tables = {"kb_settings", "kb_documents", "kb_chunks", "story_memories", "retrieval_chunks"}
    try:
        insp = inspect(engine)
        assert tables <= set(insp.get_table_names())
        fks = {fk["constrained_columns"][0]: fk for fk in insp.get_foreign_keys("story_memories")}
        assert fks["project_id"]["options"].get("ondelete") == "CASCADE"
        assert fks["chapter_id"]["options"].get("ondelete") == "SET NULL"
        assert fks["analysis_id"]["options"].get("ondelete") == "SET NULL"
        fks = {fk["constrained_columns"][0]: fk for fk in insp.get_foreign_keys("kb_chunks")}
        assert fks["document_id"]["options"].get("ondelete") == "CASCADE"
        for table in ("kb_settings", "kb_documents", "retrieval_chunks"):
            fk = insp.get_foreign_keys(table)[0]
            assert fk["referred_table"] == "projects" and fk["options"].get("ondelete") == "CASCADE"
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("INSERT INTO kb_settings (project_id) VALUES (1)"))
            row = conn.execute(text("SELECT auto_inject, top_k, min_score, max_chars FROM kb_settings")).one()
            assert tuple(row) == (1, 6, 0.35, 3000)
            conn.execute(text("INSERT INTO kb_documents (project_id, filename, sha256) VALUES (1, 'a.md', 'h')"))
            with pytest.raises(IntegrityError):
                with conn.begin_nested():
                    conn.execute(text("INSERT INTO kb_documents (project_id, filename, sha256) VALUES (1, 'b', 'h')"))
            conn.execute(text("INSERT INTO kb_chunks (document_id, seq, content) VALUES (1, 1, '块')"))
            conn.execute(
                text(
                    "INSERT INTO retrieval_chunks (project_id, source, ref_id, text, tokens) "
                    "VALUES (1, 'kb', 1, 't', '{}')"
                )
            )
            with pytest.raises(IntegrityError):
                with conn.begin_nested():
                    conn.execute(
                        text(
                            "INSERT INTO retrieval_chunks (project_id, source, ref_id, text, tokens) "
                            "VALUES (1, 'kb', 1, 't', '{}')"
                        )
                    )
            conn.execute(
                text(
                    "INSERT INTO story_memories (project_id, chapter_id, chapter_no, kind, content, origin) "
                    "VALUES (1, 1, 1, 'manual', '手动', 'manual')"
                )
            )
            # 删章节：手动记忆保留、chapter_id 置空、章号不变
            conn.execute(text("DELETE FROM chapters"))
            assert tuple(conn.execute(text("SELECT chapter_id, chapter_no FROM story_memories")).one()) == (None, 1)
            # 删文档级联分块
            conn.execute(text("DELETE FROM kb_documents"))
            assert conn.execute(text("SELECT count(*) FROM kb_chunks")).scalar() == 0
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        assert not (tables & set(inspect(engine).get_table_names()))
        with engine.begin() as conn:
            assert conn.execute(text("SELECT title FROM projects")).scalar() == "星海"
    finally:
        engine.dispose()


def test_0016_reading_progress(tmp_path):
    """0016：projects 新增 reading_progress（存量项目取 {}）；downgrade -1 删除该列且存量项目保留。"""
    db_file = tmp_path / "migrate0016.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "0015")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO projects (user_id, title, genre, premise, style, status) "
                    "VALUES (1, '星海', '科幻', '设定', '', 'planning')"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0016")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("projects")}
        assert "reading_progress" in cols
        with engine.connect() as conn:
            assert conn.execute(text("SELECT reading_progress FROM projects")).scalar_one() == "{}"
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("projects")}
        assert "reading_progress" not in cols
        with engine.connect() as conn:
            assert conn.execute(text("SELECT title FROM projects")).scalar_one() == "星海"
    finally:
        engine.dispose()


def test_0017_book_imports(tmp_path):
    """0017：新建 book_imports（user_id CASCADE）、generation_logs.kind 放宽到 32；downgrade -1 删表、删掉拆书的
    生成记录与任务、导入版本改记为 baseline，存量数据保留。"""
    db_file = tmp_path / "migrate0017.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    def log_sql(kind: str) -> str:
        return (
            "INSERT INTO generation_logs (project_id, kind, provider, model, params, messages, status, error, "
            f"output_chars, duration_ms) VALUES (1, '{kind}', '', '', '{{}}', '[]', 'success', '', 0, 0)"
        )

    command.upgrade(cfg, "0016")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO projects (user_id, title, genre, premise, style, status) "
                    "VALUES (1, '星海', '科幻', '设定', '', 'planning')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO chapters (project_id, chapter_no, title, content, status, word_count) "
                    "VALUES (1, 1, '第一章', '正文', 'done', 2)"
                )
            )
            conn.execute(text(log_sql("generate")))
    finally:
        engine.dispose()

    command.upgrade(cfg, "0017")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        fks = {fk["referred_table"]: fk for fk in insp.get_foreign_keys("book_imports")}
        assert fks["users"]["options"].get("ondelete") == "CASCADE"
        kind = next(c for c in insp.get_columns("generation_logs") if c["name"] == "kind")
        assert kind["type"].length == 32
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("INSERT INTO users (id, username, password_hash) VALUES (2, 'tmp', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO book_imports (user_id, filename, kind, text, chapters, expires_at) "
                    "VALUES (2, 'a.txt', 'txt', '正文', '[]', '2099-01-01 00:00:00')"
                )
            )
            row = conn.execute(text("SELECT size, split_mode, notices, doc_spans, revision FROM book_imports")).one()
            assert tuple(row) == (0, "auto", "[]", "[]", 0)
            conn.execute(text("DELETE FROM users WHERE id = 2"))
            assert conn.execute(text("SELECT count(*) FROM book_imports")).scalar_one() == 0
            conn.execute(text(log_sql("book_import.synthesize")))
            conn.execute(
                text(
                    "INSERT INTO tasks (user_id, project_id, kind, title, status, input, output_text, progress, "
                    "stage, error, step_cursor) VALUES (1, 1, 'book.import', '拆书', 'succeeded', '{}', '', 100, "
                    "'', '', 0)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO chapter_versions (chapter_id, seq, source, content, word_count) "
                    "VALUES (1, 1, 'import', '正文', 2)"
                )
            )
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert "book_imports" not in insp.get_table_names()
        kind = next(c for c in insp.get_columns("generation_logs") if c["name"] == "kind")
        assert kind["type"].length == 16
        with engine.connect() as conn:
            assert conn.execute(text("SELECT kind FROM generation_logs")).scalars().all() == ["generate"]
            assert conn.execute(text("SELECT count(*) FROM tasks")).scalar_one() == 0
            assert conn.execute(text("SELECT source FROM chapter_versions")).scalar_one() == "baseline"
            assert conn.execute(text("SELECT title FROM projects")).scalar_one() == "星海"
    finally:
        engine.dispose()


def test_0018_agent_foreign_keys_and_downgrade(tmp_path):
    db_file = tmp_path / "migrate0018.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "0018")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        inspector = inspect(engine)
        assert {"agent_conversations", "agent_messages", "agent_changes"} <= set(inspector.get_table_names())
        for table in ("agent_messages", "agent_changes"):
            fks = {
                fk["constrained_columns"][0]: fk["options"].get("ondelete") for fk in inspector.get_foreign_keys(table)
            }
            assert fks["conversation_id"] == "CASCADE" and fks["turn_task_id"] == "SET NULL"
            assert any(
                c["column_names"] == ["conversation_id", "turn_task_id", "tool_call_id"]
                for c in inspector.get_unique_constraints(table)
            )
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('agent-test', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO projects (user_id, title, genre, premise, style, status) "
                    "VALUES (1, '书', '', '', '', 'planning')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO chapters (project_id, chapter_no, title, content, status, word_count) "
                    "VALUES (1, 1, '一', '正文', 'done', 2)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO chapter_versions (chapter_id, seq, source, content, word_count) "
                    "VALUES (1, 1, 'agent', '正文', 2)"
                )
            )
            conn.execute(text("INSERT INTO agent_conversations (user_id, project_id, title) VALUES (1, 1, '会话')"))
            conn.execute(text("INSERT INTO agent_messages (conversation_id, role, content) VALUES (1, 'user', '问题')"))
            assert conn.execute(text("SELECT auto_execute FROM agent_conversations")).scalar() == 0
            conn.execute(text("DELETE FROM agent_conversations"))
            assert conn.execute(text("SELECT count(*) FROM agent_messages")).scalar() == 0
    finally:
        engine.dispose()
    command.downgrade(cfg, "0017")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        assert "agent_conversations" not in inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT source FROM chapter_versions")).scalar() == "manual"
    finally:
        engine.dispose()


def test_0019_skills_mcp(tmp_path):
    """0019：skills / skill_references / mcp_plugins 三张表、4 个内置全局 Skill、全局 slug 的部分唯一索引；
    agent_conversations.mcp_plugin_ids、agent_messages.skill_id（SET NULL）/ skill_snapshot；downgrade -1 全部删除。"""
    db_file = tmp_path / "migrate0019.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "0019")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        inspector = inspect(engine)
        assert {"skills", "skill_references", "mcp_plugins"} <= set(inspector.get_table_names())
        assert "mcp_plugin_ids" in {c["name"] for c in inspector.get_columns("agent_conversations")}
        fks = {
            fk["constrained_columns"][0]: fk["options"].get("ondelete")
            for fk in inspector.get_foreign_keys("agent_messages")
        }
        assert fks["skill_id"] == "SET NULL" and fks["conversation_id"] == "CASCADE"
        assert any(
            c["column_names"] == ["conversation_id", "turn_task_id", "tool_call_id"]
            for c in inspector.get_unique_constraints("agent_messages")
        )
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            builtin = (
                conn.execute(text("SELECT builtin_key FROM skills WHERE owner_user_id IS NULL ORDER BY id"))
                .scalars()
                .all()
            )
            assert builtin == ["long-continue", "deslop", "chapter-diagnose", "foreshadow-check"]
            # 全局 slug 部分唯一索引在 SQLite 上生效；用户私有 Skill 可与全局同名
            with pytest.raises(IntegrityError):
                with conn.begin_nested():
                    conn.execute(text("INSERT INTO skills (owner_user_id, slug, name) VALUES (NULL, 'deslop', 'x')"))
            conn.execute(text("INSERT INTO users (username, password_hash) VALUES ('u', 'x')"))
            conn.execute(text("INSERT INTO skills (owner_user_id, slug, name) VALUES (1, 'deslop', '我的')"))
            with pytest.raises(IntegrityError):
                with conn.begin_nested():
                    conn.execute(text("INSERT INTO skills (owner_user_id, slug, name) VALUES (1, 'deslop', '重复')"))
            sid = conn.execute(text("SELECT id FROM skills WHERE owner_user_id = 1")).scalar_one()
            conn.execute(
                text(f"INSERT INTO skill_references (skill_id, title, content) VALUES ({sid}, '资料', '内容')")
            )
            conn.execute(
                text("INSERT INTO mcp_plugins (user_id, slug, name, transport, url) VALUES (1, 'p', 'P', 'sse', 'x')")
            )
            conn.execute(text("DELETE FROM skills WHERE id = :sid"), {"sid": sid})
            assert conn.execute(text("SELECT count(*) FROM skill_references")).scalar() == 0
            conn.execute(text("DELETE FROM users"))
            assert conn.execute(text("SELECT count(*) FROM mcp_plugins")).scalar() == 0
    finally:
        engine.dispose()
    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        inspector = inspect(engine)
        assert not ({"skills", "skill_references", "mcp_plugins"} & set(inspector.get_table_names()))
        assert "skill_id" not in {c["name"] for c in inspector.get_columns("agent_messages")}
        assert "mcp_plugin_ids" not in {c["name"] for c in inspector.get_columns("agent_conversations")}
    finally:
        engine.dispose()


def test_0020_workshop_constraints_and_downgrade(tmp_path):
    """工坊迁移保留已发布内容、级联提交与互动，pending 更新由部分唯一索引保护。"""
    db_file = tmp_path / "migrate0020.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "0019")
    command.upgrade(cfg, "0020")
    engine = create_engine(f"sqlite:///{db_file}")
    tables = {"workshop_items", "workshop_submissions", "workshop_likes", "workshop_imports"}
    try:
        insp = inspect(engine)
        assert tables <= set(insp.get_table_names())
        assert any(
            i["name"] == "uq_workshop_pending_item" and i["unique"] for i in insp.get_indexes("workshop_submissions")
        )
        author_fk = next(
            f for f in insp.get_foreign_keys("workshop_items") if f["constrained_columns"] == ["author_id"]
        )
        assert author_fk["options"]["ondelete"] == "SET NULL"
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("INSERT INTO users(id, username, password_hash) VALUES (1, 'author', 'x')"))
            conn.execute(
                text(
                    "INSERT INTO workshop_items(id, kind, name, category, content, author_id, like_count) "
                    "VALUES (1, 'style', 'shared', 'general', '{}', 1, 3)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO workshop_submissions(item_id, submitter_id, kind, name, category, content) "
                    "VALUES (1, 1, 'style', 'update', 'general', '{}')"
                )
            )
            conn.execute(text("INSERT INTO workshop_likes VALUES (1, 1)"))
            conn.execute(text("INSERT INTO workshop_imports VALUES (1, 1)"))
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO workshop_submissions(item_id, submitter_id, kind, name, category, content) "
                    "VALUES (1, 1, 'style', 'duplicate', 'general', '{}')"
                )
            )
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = 1"))
            assert conn.execute(text("SELECT author_id, like_count FROM workshop_items")).one() == (None, 3)
            for table in tables - {"workshop_items"}:
                assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
    finally:
        engine.dispose()
    command.downgrade(cfg, "0019")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        assert not tables & set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_0023_admin_audit_snapshots_no_cascade_no_reused_ids(tmp_path):
    db_file = tmp_path / "migrate0023.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "0022")
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, username, password_hash) VALUES (1, 'historical', 'x')"))
    engine.dispose()
    command.upgrade(cfg, "0023")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(engine)
        assert insp.get_foreign_keys("admin_audit_logs") == []
        assert {i["name"] for i in insp.get_indexes("admin_audit_logs")} == {
            "ix_admin_audit_time", "ix_admin_audit_actor_time", "ix_admin_audit_target_time",
        }
        insert = text(
            "INSERT INTO admin_audit_logs "
            "(operation_id,occurred_at,actor_user_id,actor_username,action,result,target_type,target_id,summary) "
            "VALUES (:operation,'2026-01-01 00:00:00',1,'historical','user.delete','success','user',1,'{}')"
        )
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(insert, {"operation": "first"})
            conn.execute(text("DELETE FROM users WHERE id=1"))
            assert conn.execute(text("SELECT actor_username FROM admin_audit_logs")).scalar_one() == "historical"
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, {"operation": "first"})
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM admin_audit_logs"))
            conn.execute(insert, {"operation": "second"})
            assert conn.execute(text("SELECT id FROM admin_audit_logs")).scalar_one() == 2
    finally:
        engine.dispose()
    command.downgrade(cfg, "0022")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        assert "admin_audit_logs" not in inspect(engine).get_table_names()
        assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
