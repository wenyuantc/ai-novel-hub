"""0026 ADD COLUMN保留父表/子引用；旧时间null及pending降级屏障。"""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from tests.test_admin_dashboard_migrations import _children, _database


def test_0026_columns_preserve_children_and_null_clocks(tmp_path):
    cfg, path = _database(tmp_path)
    command.upgrade(cfg, "0025")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','x')"))
            conn.execute(text(
                "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                "VALUES(1,1,'作品','','','','planning')"
            ))
            conn.execute(text(
                "INSERT INTO tasks(id,user_id,project_id,kind,status,input) "
                "VALUES(1,1,1,'chapter.generate','queued','{}')"
            ))
            conn.execute(text(
                "INSERT INTO tasks(id,user_id,project_id,kind,status,input,retry_of_id) "
                "VALUES(2,1,1,'chapter.generate','failed','{}',1)"
            ))
            conn.execute(text(
                "INSERT INTO generation_logs(id,project_id,kind,provider,model,params,messages,status,error,"
                "output_chars,duration_ms,task_id) VALUES(1,1,'generate','','','{}','[]','success','',0,0,1)"
            ))
            conn.execute(text("INSERT INTO agent_conversations(id,user_id,project_id) VALUES(1,1,1)"))
            conn.execute(text(
                "INSERT INTO agent_messages(id,conversation_id,turn_task_id,role,content) VALUES(1,1,1,'user','消息')"
            ))
            before = _children(conn)
            rootpage = conn.execute(text("SELECT rootpage FROM sqlite_master WHERE name='tasks'")).scalar_one()
        command.upgrade(cfg, "0026")
        with engine.connect() as conn:
            assert _children(conn) == before
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
            assert conn.execute(text("SELECT rootpage FROM sqlite_master WHERE name='tasks'")).scalar_one() == rootpage
            assert conn.execute(text(
                "SELECT queued_at,admin_cancel_operation_id,admin_cancel_requested_at FROM tasks"
            )).all() == [(None, None, None), (None, None, None)]
        assert "ix_tasks_admin_cancel_operation_id" in {i["name"] for i in inspect(engine).get_indexes("tasks")}
        command.downgrade(cfg, "0025")
        with engine.connect() as conn:
            assert _children(conn) == before
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()


def test_0026_rejects_downgrade_with_pending(tmp_path):
    cfg, path = _database(tmp_path)
    command.upgrade(cfg, "0026")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','x')"))
            conn.execute(text(
                "INSERT INTO tasks(user_id,kind,status,input,admin_cancel_operation_id) "
                "VALUES(1,'agent.turn','queued','{}','00000000-0000-0000-0000-000000000001')"
            ))
        with pytest.raises(RuntimeError, match="尚未收尾"):
            command.downgrade(cfg, "0025")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0026"
    finally:
        engine.dispose()
