"""富文档迁移：真实 0039 存量、空结构、跨场景引用及安全降级。"""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.models import Base
from tests.test_scenes_migration import config_for


@pytest.fixture
def rich_migration(tmp_path):
    path = tmp_path / "rich-upgrade.db"
    config = config_for(path)
    command.upgrade(config, "0039")
    engine = create_engine(f"sqlite:///{path}")
    original = " \t甲😀\r\n\n乙e\u0301\r尾  "
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'writer','hash')"))
        conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                          "VALUES(1,1,'旧书','','','','planning')"))
        conn.execute(text("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count,"
                          "content_revision,updated_at) VALUES(1,1,1,'旧章',:body,'draft',8,7,'2026-01-01 01:02:03')"),
                     {"body": original})
        conn.execute(text("INSERT INTO scenes(id,chapter_id,content,sort,active,updated_at) "
                          "VALUES(1,1,:body,0,1,'2026-01-01 01:02:03'),(2,1,'另一景',1,0,'2026-01-01 01:02:03')"),
                     {"body": original})
    engine.dispose()
    command.upgrade(config, "0040")
    yield config, path, original


def test_old_scene_is_unchanged_and_empty_structure_downgrades(rich_migration):
    config, path, original = rich_migration
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        scene = conn.execute(text("SELECT content,updated_at FROM scenes WHERE id=1")).one()
        chapter = conn.execute(text("SELECT content,content_revision,updated_at FROM chapters WHERE id=1")).one()
        assert scene.content == chapter.content == original
        assert scene.updated_at == chapter.updated_at == "2026-01-01 01:02:03"
        assert chapter.content_revision == 7
        for table in ("scene_documents", "scene_sections", "scene_markers", "scene_review_proposals",
                      "scene_review_payload_chunks", "scene_selection_receipts", "scene_review_analysis_receipts"):
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
    engine.dispose()
    command.downgrade(config, "0039")
    engine = create_engine(f"sqlite:///{path}")
    assert "scene_sections" not in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT content FROM scenes WHERE id=1")).scalar_one() == original
    engine.dispose()


def test_fresh_rich_schema_matches_models(rich_migration):
    _config, path, _original = rich_migration
    engine = create_engine(f"sqlite:///{path}")
    inspector = inspect(engine)
    for name in ("scene_documents", "scene_sections", "scene_markers", "scene_review_proposals",
                 "scene_review_payload_chunks", "scene_selection_receipts", "scene_review_analysis_receipts"):
        assert set(Base.metadata.tables[name].c.keys()) == {column["name"] for column in inspector.get_columns(name)}
        expected = {tuple(fk.parent.name for fk in constraint.elements)
                    for constraint in Base.metadata.tables[name].foreign_key_constraints}
        actual = {tuple(fk["constrained_columns"]) for fk in inspector.get_foreign_keys(name)}
        assert actual == expected
    engine.dispose()


def test_cross_scene_marker_reference_and_duplicate_section_key_rejected(rich_migration):
    _config, path, _original = rich_migration
    engine = create_engine(f"sqlite:///{path}")
    key = "645d44c0-cc16-4949-973b-542022da8421"
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.execute(text("INSERT INTO scene_sections(id,scene_id,logical_key,anchor,sort,title,content) "
                          "VALUES(10,1,:key,1,0,'备选','作者材料')"), {"key": key})
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(text("INSERT INTO scene_sections(scene_id,logical_key,anchor,sort,title,content) "
                              "VALUES(1,:key,1,1,'重复','')"), {"key": key})
    # 标记即使拿到了另一景的真实 section ID，也不能跨景引用；延迟外键在事务提交时验证。
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.execute(text("INSERT INTO scene_markers(scene_id,logical_key,section_id,status,color,"
                          "start_offset,end_offset) VALUES(2,'d4baf738-d1ca-4d51-a9cc-22fe47213105',"
                          "10,'active','yellow',0,1)"))
    engine.dispose()


def test_downgrade_rejects_any_new_author_data_without_dropping_tables(rich_migration):
    config, path, _original = rich_migration
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO scene_sections(scene_id,logical_key,anchor,sort,title,content) "
                          "VALUES(1,'645d44c0-cc16-4949-973b-542022da8421',1,0,'备选','保留')"))
    engine.dispose()
    with pytest.raises(RuntimeError, match="不能无损降级"):
        command.downgrade(config, "0039")
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT content FROM scene_sections")).scalar_one() == "保留"
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0040"
    engine.dispose()


def test_selection_receipt_survives_project_delete_but_not_user_delete_and_blocks_downgrade(rich_migration):
    config, path, _original = rich_migration
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.execute(text("INSERT INTO scene_selection_receipts(request_key,user_id) "
                          "VALUES('0981fe8e-f4cc-4336-80a2-df11c2812834',1)"))
        conn.execute(text("DELETE FROM projects WHERE id=1"))
        assert conn.execute(text("SELECT count(*) FROM scene_selection_receipts")).scalar_one() == 1
    engine.dispose()
    with pytest.raises(RuntimeError, match="不能无损降级"):
        command.downgrade(config, "0039")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.execute(text("DELETE FROM users WHERE id=1"))
        assert conn.execute(text("SELECT count(*) FROM scene_selection_receipts")).scalar_one() == 0
    engine.dispose()
