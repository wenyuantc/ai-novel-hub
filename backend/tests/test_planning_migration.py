"""0034 原位升级、模型一致、级联与有数据降级保护。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.models import Base

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_0034_preserves_existing_story_and_guards_downgrade(tmp_path):
    path = tmp_path / "planning.db"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    config.attributes["configure_logger"] = False
    command.upgrade(config, "0033")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'legacy','hash')"))
        conn.execute(
            text(
                "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                "VALUES (1,1,'旧作品','','','','planning'),(2,1,'保留作品','','','','planning')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                "VALUES (1,1,1,'旧章','不可变的正文','draft',6)"
            )
        )
    engine.dispose()
    command.upgrade(config, "0034")
    engine = create_engine(f"sqlite:///{path}")
    inspector = inspect(engine)
    tables = {name for name in Base.metadata.tables if name.startswith(("planning_", "subplot"))
              and name != "planning_unit_characters"}
    assert len(tables) == 11
    for name in tables:
        future = {"scene_id", "source_level", *(f"{field}_mode" for field in (
            "pov", "tags", "character", "world", "organization", "career", "relationship",
        ))} if name == "planning_units" else set()
        assert set(Base.metadata.tables[name].c.keys()) - future == {c["name"] for c in inspector.get_columns(name)}
        old_fks = {fk for fk in Base.metadata.tables[name].foreign_keys if fk.parent.name not in future}
        assert len(old_fks) == len(inspector.get_foreign_keys(name))
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        assert conn.execute(text("SELECT content FROM chapters WHERE id=1")).scalar_one() == "不可变的正文"
        assert conn.execute(text("SELECT planning_revision FROM projects WHERE id=1")).scalar_one() == 0
        conn.execute(text("INSERT INTO planning_units(id,project_id,chapter_id) VALUES (1,1,1)"))
        conn.execute(text("INSERT INTO subplots(id,project_id,name) VALUES (1,1,'线甲'),(2,1,'线乙')"))
        conn.execute(
            text(
                "INSERT INTO subplot_crossings(id,project_id,unit_id,title,description) "
                "VALUES (1,1,1,'相遇','交换线索')"
            )
        )
        conn.execute(text("INSERT INTO subplot_crossing_participants(crossing_id,subplot_id) VALUES (1,1),(1,2)"))
        assert not conn.execute(text("PRAGMA foreign_key_check")).all()
    engine.dispose()
    with pytest.raises(RuntimeError, match="不能无损降级"):
        command.downgrade(config, "0033")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(text("DELETE FROM projects WHERE id=1"))
        assert conn.execute(text("SELECT count(*) FROM subplot_crossing_participants")).scalar_one() == 0
        assert conn.execute(text("SELECT title FROM projects WHERE id=2")).scalar_one() == "保留作品"
    engine.dispose()
    command.downgrade(config, "0033")
    engine = create_engine(f"sqlite:///{path}")
    assert "planning_units" not in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT title FROM projects WHERE id=2")).scalar_one() == "保留作品"
    engine.dispose()
