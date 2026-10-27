from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

BACKEND_DIR = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {"users", "ai_configs", "projects", "world_settings", "characters", "outlines", "chapters"}


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
