"""批次记录的迁移保留历史身份，并阻止有数据时降级。"""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.models import Base
from tests.test_consistency_migration import _engine
from tests.test_setting_fields_migration import _config, _schema_contract


def test_batch_revision_schema_and_history_retention(tmp_path):
    path = tmp_path / "revisions.sqlite3"
    config = _config(path)
    command.upgrade(config, "0060")
    engine = _engine(path)
    reference = create_engine("sqlite://")
    try:
        Base.metadata.create_all(reference)
        for table in ("revision_batches", "revision_batch_scenes"):
            assert _schema_contract(inspect(engine), table) == _schema_contract(inspect(reference), table)
        with engine.begin() as db:
            db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'author','hash')"))
            db.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                            "VALUES (1,1,'长篇','','','','planning')"))
            db.execute(text("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                            "VALUES (1,1,1,'初见','青石镇','draft',3)"))
            db.execute(text("INSERT INTO scenes(id,chapter_id) VALUES (1,1)"))
            db.execute(text("INSERT INTO revision_batches"
                            "(id,project_id,query,replacement,status,chapter_count,hit_count)"
                            " VALUES (1,1,'镇','城','applied',1,1)"))
            db.execute(text("INSERT INTO revision_batch_scenes(batch_id,chapter_id,scene_id,chapter_slot,"
                            "chapter_no_at_apply,chapter_title,applied_revision,before_document,"
                            "start_offsets,hit_count) VALUES(1,1,1,1,1,'初见',1,'{}','[2]',1)"))
            db.execute(text("DELETE FROM chapters WHERE id=1"))
            assert db.execute(text("SELECT chapter_id,scene_id,chapter_no_at_apply,chapter_title "
                                   "FROM revision_batch_scenes")).one() == (None, None, 1, "初见")
        with pytest.raises(RuntimeError, match="仍有批量修订历史"):
            command.downgrade(config, "0059")
        with engine.begin() as db:
            db.execute(text("DELETE FROM projects WHERE id=1"))
            assert db.scalar(text("SELECT count(*) FROM revision_batch_scenes")) == 0
        command.downgrade(config, "0059")
        assert "revision_batches" not in inspect(engine).get_table_names()
        command.upgrade(config, "0060")
    finally:
        engine.dispose()
        reference.dispose()
