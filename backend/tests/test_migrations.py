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
