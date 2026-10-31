"""0025只增加统计存储，不重建任务父表或丢失已记录历史。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

BACKEND = Path(__file__).resolve().parents[1]
TABLES = {"admin_dashboard_tracking", "admin_dashboard_daily", "admin_task_statistics"}


def _database(tmp_path):
    path = tmp_path / "dashboard-migration.db"
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    cfg.attributes["configure_logger"] = False
    return cfg, path


def _children(conn):
    return (
        conn.execute(text("SELECT id, retry_of_id FROM tasks ORDER BY id")).all(),
        conn.execute(text("SELECT id, task_id FROM generation_logs")).all(),
        conn.execute(text("SELECT id, turn_task_id, content FROM agent_messages")).all(),
    )


def test_0025_upgrade_and_empty_statistics_downgrade_keep_task_children(tmp_path):
    cfg, path = _database(tmp_path)
    command.upgrade(cfg, "0024")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("INSERT INTO users (id,username,password_hash) VALUES (1,'owner','x')"))
            conn.execute(text(
                "INSERT INTO projects (id,user_id,title,genre,premise,style,status) "
                "VALUES (1,1,'原作品','','','','planning')"
            ))
            conn.execute(text(
                "INSERT INTO tasks (id,user_id,project_id,kind,status,input) "
                "VALUES (1,1,1,'chapter.analyze','failed','{}')"
            ))
            conn.execute(text(
                "INSERT INTO tasks (id,user_id,project_id,kind,status,input,retry_of_id) "
                "VALUES (2,1,1,'chapter.analyze','succeeded','{}',1)"
            ))
            conn.execute(text(
                "INSERT INTO generation_logs "
                "(id,project_id,kind,provider,model,params,messages,status,error,output_chars,duration_ms,task_id) "
                "VALUES (1,1,'generate','','','{}','[]','success','',0,0,1)"
            ))
            conn.execute(text("INSERT INTO agent_conversations (id,user_id,project_id) VALUES (1,1,1)"))
            conn.execute(text(
                "INSERT INTO agent_messages (id,conversation_id,turn_task_id,role,content) "
                "VALUES (1,1,1,'user','保留的消息')"
            ))
            before = _children(conn)
        command.upgrade(cfg, "0025")
        assert TABLES <= set(inspect(engine).get_table_names())
        assert "statistics_key" in {column["name"] for column in inspect(engine).get_columns("tasks")}
        for name in TABLES:
            assert inspect(engine).get_foreign_keys(name) == []
        with engine.connect() as conn:
            assert _children(conn) == before
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
        command.downgrade(cfg, "0024")
        assert not TABLES & set(inspect(engine).get_table_names())
        assert "statistics_key" not in {column["name"] for column in inspect(engine).get_columns("tasks")}
        with engine.connect() as conn:
            assert _children(conn) == before
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("history", ["task", "zero_snapshot"])
def test_0025_rejects_downgrade_with_recorded_statistics(tmp_path, history):
    cfg, path = _database(tmp_path)
    command.upgrade(cfg, "0025")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            if history == "task":
                conn.execute(text(
                    "INSERT INTO admin_task_statistics (key,kind,origin,status,created_event_at) "
                    "VALUES ('00000000-0000-0000-0000-000000000001','chapter.analyze','new','queued',CURRENT_TIMESTAMP)"
                ))
            else:
                conn.execute(text(
                    "INSERT INTO admin_dashboard_daily "
                    "(day,captured_at,users,projects,chapters,total_words,pending_submissions) "
                    "VALUES ('2026-10-31',CURRENT_TIMESTAMP,0,0,0,0,0)"
                ))
        with pytest.raises(RuntimeError, match="拒绝丢失历史"):
            command.downgrade(cfg, "0024")
        assert TABLES <= set(inspect(engine).get_table_names())
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0025"
            table = "admin_task_statistics" if history == "task" else "admin_dashboard_daily"
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 1
    finally:
        engine.dispose()
