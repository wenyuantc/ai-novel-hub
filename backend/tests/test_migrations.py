from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

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

    command.upgrade(cfg, "head")
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
