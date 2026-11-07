"""真正从0038升级；FK打开时全部旧行／身份高水位／原稿与时间戳无损。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError

from app.models import Base

BACKEND = Path(__file__).resolve().parents[1]


def config_for(path):
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    config.attributes["configure_logger"] = False
    return config


def _foreign_keys_on(connection, _record):
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def test_0039_preserves_old_graph_and_sequences(tmp_path):
    path = tmp_path / "scenes-upgrade.db"
    config = config_for(path)
    command.upgrade(config, "0038")
    engine = create_engine(f"sqlite:///{path}")
    original = " \t甲👩‍🚀\r\n\n乙\u3000尾  "
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        statements = [
            "INSERT INTO users(id,username,password_hash) VALUES(1,'legacy','hash')",
            "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
            "VALUES(1,1,'原书','','','','planning'),(2,1,'回收站','','','','planning')",
            "UPDATE projects SET deleted_at='2026-10-01 01:02:03' WHERE id=2",
            "INSERT INTO outlines(id,project_id,chapter_no,title,summary,sort,volume_no,volume_title) "
            "VALUES(7,1,1,'原纲','原摘要',0,1,'卷一')",
            "INSERT INTO chapters(id,project_id,outline_id,chapter_no,title,content,status,word_count,"
            "created_at,updated_at) "
            "VALUES(17,1,7,1,'旧章',:content,'done',99,'2026-01-01 01:02:03','2026-02-02 03:04:05'),"
            "(18,2,NULL,1,'空章','','draft',0,'2026-01-01 01:02:03','2026-02-02 03:04:05')",
            "INSERT INTO chapter_plans(id,project_id,outline_id,seq,title,chapter_id) VALUES(8,1,7,1,'细纲',17)",
            "UPDATE chapters SET plan_id=8 WHERE id=17",
            "INSERT INTO characters(id,project_id,name,role_type,profile,relations_text,sort) "
            "VALUES(11,1,'角色','','','',0)",
            "INSERT INTO chapter_characters(chapter_id,character_id) VALUES(17,11)",
            "INSERT INTO generation_logs(id,project_id,chapter_id,kind,status,params,messages,"
            "provider,model,error,output_chars,duration_ms) "
            "VALUES(6,1,17,'generate','success','{}','[]','openai_compat','model','',0,0)",
            "INSERT INTO chapter_versions(id,chapter_id,seq,source,content,word_count,generation_log_id) "
            "VALUES(9,17,1,'manual','旧历史',3,6)",
            "INSERT INTO setting_timelines(id,project_id,subject_kind,character_id,baseline) "
            "VALUES(3,1,'character',11,'{}')",
            "INSERT INTO setting_progressions(id,timeline_id,field_key,operation,value,anchor_kind,chapter_id) "
            "VALUES(4,3,'current_state','replace','\"旧变化\"','chapter',17)",
            "INSERT INTO planning_units(id,project_id,chapter_id,plan_id,pov_character_id) VALUES(5,1,17,8,11)",
            "INSERT INTO planning_units(id,project_id) VALUES(90,1)",
            "DELETE FROM planning_units WHERE id=90",
            "INSERT INTO planning_tags(id,project_id,name) VALUES(2,1,'标签')",
            "INSERT INTO planning_unit_tags(unit_id,tag_id) VALUES(5,2)",
            "INSERT INTO subplots(id,project_id,name) VALUES(1,1,'主线'),(2,1,'暗线')",
            "INSERT INTO subplot_nodes(id,subplot_id,unit_id,content) VALUES(1,1,5,'原节点')",
            "INSERT INTO subplot_crossings(id,project_id,unit_id,title,description) VALUES(1,1,5,'交汇','原交汇')",
            "INSERT INTO subplot_crossing_participants(crossing_id,subplot_id) VALUES(1,1),(1,2)",
            "INSERT INTO outline_snapshots(project_id,reason,note,data) VALUES(1,'manual','历史','{}')",
        ]
        for statement in statements:
            conn.execute(text(statement), {"content": original})
        tables = inspect(conn).get_table_names()
        columns = {table: [column["name"] for column in inspect(conn).get_columns(table)] for table in tables}
        before = {table: conn.exec_driver_sql(f'SELECT * FROM "{table}"').all()
                  for table in tables if table not in ("alembic_version", "sqlite_sequence")}
        assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    engine.dispose()
    event.listen(Engine, "connect", _foreign_keys_on)
    try:
        command.upgrade(config, "0039")
    finally:
        event.remove(Engine, "connect", _foreign_keys_on)
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            for table, rows in before.items():
                names = ",".join(f'"{column}"' for column in columns[table])
                assert conn.exec_driver_sql(f'SELECT {names} FROM "{table}"').all() == rows, table
            scenes = conn.execute(text(
                "SELECT chapter_id,content,sort,separator_before,created_at,updated_at FROM scenes ORDER BY chapter_id"
            )).all()
            assert [(row.chapter_id, row.content, row.sort, row.separator_before) for row in scenes] == [
                (17, original, 0, ""), (18, "", 0, ""),
            ]
            assert scenes[1].created_at == "2026-01-01 01:02:03.000000"
            assert conn.execute(text("SELECT content_revision FROM chapters")).scalars().all() == [0, 0]
            assert conn.execute(text("SELECT scene_snapshot FROM chapter_versions")).scalar_one() is None
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
            conn.execute(text("INSERT INTO planning_units(project_id) VALUES(1)"))
            assert conn.execute(text("SELECT max(id) FROM planning_units")).scalar_one() > 90
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(text("INSERT INTO scenes(chapter_id,sort) VALUES(17,0)"))
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(text("INSERT INTO planning_units(project_id,scene_id,source_level,chapter_id) "
                                  "VALUES(1,1,'scene',17)"))
            conn.execute(text("DELETE FROM projects WHERE id=1"))
            assert conn.execute(text("SELECT count(*) FROM scenes WHERE chapter_id=17")).scalar_one() == 0
            assert conn.execute(text("SELECT content FROM chapters WHERE id=18")).scalar_one() == ""
    finally:
        engine.dispose()


def test_0039_fresh_schema_matches_models_and_safe_downgrade(tmp_path):
    path = tmp_path / "scenes-fresh.db"
    config = config_for(path)
    command.upgrade(config, "0039")
    engine = create_engine(f"sqlite:///{path}")
    inspector = inspect(engine)
    for name in ("scenes", "planning_unit_characters", "planning_units", "setting_progressions"):
        assert set(Base.metadata.tables[name].c.keys()) == {column["name"] for column in inspector.get_columns(name)}
        assert len(Base.metadata.tables[name].foreign_keys) == len(inspector.get_foreign_keys(name))
    engine.dispose()
    command.downgrade(config, "0038")
    engine = create_engine(f"sqlite:///{path}")
    assert "scenes" not in inspect(engine).get_table_names()
    engine.dispose()


def test_0039_refuses_lossy_downgrade_before_dropping_anything(tmp_path):
    path = tmp_path / "scenes-guard.db"
    config = config_for(path)
    command.upgrade(config, "0039")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'writer','hash')"))
        conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                          "VALUES(1,1,'作者稿','','','','planning')"))
        conn.execute(text("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                          "VALUES(1,1,1,'新章','甲乙','draft',2)"))
        conn.execute(text("INSERT INTO scenes(chapter_id,sort,content) VALUES(1,0,'甲'),(1,1,'乙')"))
    engine.dispose()
    with pytest.raises(RuntimeError, match="不能无损降级"):
        command.downgrade(config, "0038")
    engine = create_engine(f"sqlite:///{path}")
    assert "planning_unit_characters" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM scenes")).scalar_one() == 2
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0039"
    engine.dispose()
