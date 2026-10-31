"""带真实父子存量数据验证 SQLite 迁移，不用空库升级代替数据保留证明。"""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext import asyncio as async_sqlalchemy

BACKEND = Path(__file__).resolve().parents[1]


def _config(path):
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    config.attributes["configure_logger"] = False
    return config


def _seed(path):
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(text("INSERT INTO users (id, username, password_hash) VALUES (1,'owner','x')"))
        conn.execute(
            text(
                "INSERT INTO projects (id,user_id,title,genre,premise,style,status) "
                "VALUES (1,1,'原作品','','','','writing')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO chapters (id,project_id,chapter_no,title,content,status,word_count) "
                "VALUES (1,1,1,'章','原正文','draft',3)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO chapter_versions (id,chapter_id,seq,source,content,word_count) "
                "VALUES (1,1,1,'manual','原版本',3)"
            )
        )
        conn.execute(text("INSERT INTO project_covers (project_id,mime_type,data) VALUES (1,'image/png',X'010203')"))
        conn.execute(
            text("INSERT INTO api_keys (id,user_id,name,key_hash,key_masked) VALUES (1,1,'授权','hash','mask')")
        )
        conn.execute(text("INSERT INTO api_key_projects (api_key_id,project_id) VALUES (1,1)"))
        insert = text(
            "INSERT INTO admin_audit_logs "
            "(id,operation_id,occurred_at,actor_user_id,actor_username,action,result,target_type,summary) "
            "VALUES (:id,:op,'2026-01-01 00:00:00',1,'历史管理员','project.update','success','project','{}')"
        )
        conn.execute(insert, {"id": 1, "op": "kept"})
        conn.execute(insert, {"id": 10, "op": "removed"})
        conn.execute(text("DELETE FROM admin_audit_logs WHERE id=10"))
    engine.dispose()


@pytest.mark.parametrize("foreign_keys", [False, True])
def test_upgrade_preserves_children_audit_sequence_and_restrict_then_safe_downgrade(
    tmp_path, monkeypatch, foreign_keys
):
    path = tmp_path / "recycle.db"
    config = _config(path)
    command.upgrade(config, "0023")
    _seed(path)
    if foreign_keys:
        factory = async_sqlalchemy.async_engine_from_config

        def enabled(*args, **kwargs):
            engine = factory(*args, **kwargs)

            @event.listens_for(engine.sync_engine, "connect")
            def pragma(connection, record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

            return engine

        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", enabled)
    command.upgrade(config, "0024")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        assert conn.execute(text("SELECT content FROM chapters")).scalar_one() == "原正文"
        assert conn.execute(text("SELECT content FROM chapter_versions")).scalar_one() == "原版本"
        assert conn.execute(text("SELECT data FROM project_covers")).scalar_one() == b"\x01\x02\x03"
        assert conn.execute(text("SELECT count(*) FROM api_key_projects")).scalar_one() == 1
        assert conn.execute(text("SELECT deleted_at FROM projects")).scalar_one() is None
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
        assert conn.execute(text("SELECT seq FROM sqlite_sequence WHERE name='admin_audit_logs'")).scalar_one() == 10
        checks = {entry["name"] for entry in inspect(conn).get_check_constraints("projects")}
        assert {"ck_projects_deletion_state", "ck_projects_deletion_intent"} <= checks
        fk = next(entry for entry in inspect(conn).get_foreign_keys("projects") if entry["referred_table"] == "users")
        assert fk["options"]["ondelete"] == "RESTRICT"
        insert_project = text(
            "INSERT INTO projects (user_id,title,genre,premise,style,status) VALUES (1,'临时作品','','','','planning')"
        )
        conn.execute(insert_project)
        highest = conn.execute(text("SELECT MAX(id) FROM projects")).scalar_one()
        conn.execute(text("DELETE FROM projects WHERE id=:id"), {"id": highest})
        conn.execute(insert_project)
        newer = conn.execute(text("SELECT MAX(id) FROM projects")).scalar_one()
        assert newer > highest
        conn.execute(text("DELETE FROM projects WHERE id=:id"), {"id": newer})
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(text("DELETE FROM users WHERE id=1"))
    engine.dispose()
    command.downgrade(config, "0023")
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT content FROM chapters")).scalar_one() == "原正文"
        assert conn.execute(text("SELECT content FROM chapter_versions")).scalar_one() == "原版本"
        assert "deleted_at" not in {entry["name"] for entry in inspect(conn).get_columns("projects")}
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


@pytest.mark.parametrize("state", ["trash", "pending", "high_water", "system", "restore", "soft_summary"])
def test_downgrade_refuses_to_resurrect_data_or_destroy_lifecycle_audit(tmp_path, state):
    path = tmp_path / "protected.db"
    config = _config(path)
    command.upgrade(config, "0023")
    _seed(path)
    command.upgrade(config, "0024")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        if state == "trash":
            conn.execute(text("UPDATE projects SET deleted_at='2026-01-01 00:00:00'"))
        elif state == "pending":
            conn.execute(text("UPDATE projects SET deletion_started_at='2026-01-01 00:00:00',deletion_token='pending'"))
        elif state == "system":
            conn.execute(
                text(
                    "UPDATE admin_audit_logs SET actor_user_id=NULL,actor_username='系统自动清理',"
                    "action='project.expire'"
                )
            )
        elif state == "high_water":
            conn.execute(text("UPDATE projects SET last_deleted_at='2026-01-01 00:00:00'"))
        elif state == "restore":
            conn.execute(text("UPDATE admin_audit_logs SET action='project.restore'"))
        else:
            conn.execute(
                text("UPDATE admin_audit_logs SET summary=:summary"),
                {"summary": json.dumps({"lifecycle_action": "soft_delete"})},
            )
    engine.dispose()
    with pytest.raises(RuntimeError, match="拒绝"):
        command.downgrade(config, "0023")
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0024"
        assert conn.execute(text("SELECT content FROM chapters")).scalar_one() == "原正文"
    engine.dispose()
