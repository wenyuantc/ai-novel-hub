"""真实迁移验证模板身份和创建收据的独立生命周期。"""


import pytest
from alembic import command
from sqlalchemy import create_engine, event, inspect, text

from tests.test_setting_fields_migration import _config, _foreign_keys_during_migrations


def test_0057_template_ids_and_receipts_survive_result_deletion(tmp_path, monkeypatch):
    _foreign_keys_during_migrations(monkeypatch)
    path = tmp_path / "novel-template-migration.sqlite3"
    config = _config(path)
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        command.upgrade(config, "0056")
        with engine.begin() as db:
            db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'author','hash')"))
            db.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                            "VALUES(1,1,'原书','','','','planning')"))
        command.upgrade(config, "0057")
        with engine.begin() as db:
            assert db.scalar(text("SELECT title FROM projects WHERE id=1")) == "原书"
            ddl = db.scalar(text("SELECT sql FROM sqlite_master WHERE name='novel_templates'"))
            assert "AUTOINCREMENT" in ddl.upper()
            db.execute(text("INSERT INTO novel_templates(owner_id,name,description,snapshot,counts,source_title) "
                            "VALUES(1,'旧模板','','{}','{}','原书')"))
            old_id = db.scalar(text("SELECT id FROM novel_templates WHERE name='旧模板'"))
            db.execute(text("DELETE FROM novel_templates WHERE id=:id"), {"id": old_id})
            db.execute(text("INSERT INTO novel_templates(owner_id,name,description,snapshot,counts,source_title) "
                            "VALUES(1,'新模板','','{}','{}','原书')"))
            assert db.scalar(text("SELECT id FROM novel_templates WHERE name='新模板'")) > old_id
        # 先分别验证模板和图片存在时拒绝降级，再验证只剩成功收据时也拒绝降级。
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "0056")
        with engine.begin() as db:
            db.execute(text("INSERT INTO novel_template_images"
                            "(template_id,local_ref,mime_type,data,width,height,byte_size,sha256) "
                            "SELECT id,'character.1','image/webp',:data,1,1,1,:digest FROM novel_templates"),
                       {"data": b"x", "digest": "a" * 64})
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "0056")
        with engine.begin() as db:
            db.execute(text("INSERT INTO novel_template_creation_receipts"
                            "(owner_id,intent_key,request_version,request_digest,result_project_id) "
                            "VALUES(1,'00000000-0000-4000-8000-000000000001',1,:digest,1)"),
                       {"digest": "a" * 64})
            db.execute(text("DELETE FROM novel_templates"))
            db.execute(text("DELETE FROM projects WHERE id=1"))
            assert db.scalar(text("SELECT result_project_id FROM novel_template_creation_receipts")) is None
            assert db.scalar(text("SELECT count(*) FROM novel_template_creation_receipts")) == 1
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "0056")
        with engine.begin() as db:
            db.execute(text("DELETE FROM novel_template_creation_receipts"))
            assert not db.exec_driver_sql("PRAGMA foreign_key_check").all()
        command.downgrade(config, "0056")
        with engine.connect() as db:
            assert "novel_templates" not in inspect(db).get_table_names()
    finally:
        engine.dispose()
