import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import CheckConstraint, func, inspect, select, text, update

from app.models import Chapter, Project, User, WritingDailyStat, WritingGoal, WritingStatsTracking
from tests.helpers import create_project, register
from tests.test_project_transfer import _backup, _import
from tests.test_task_worker_migration import migration_db as migration_db


def _assert_schema(conn):
    inspector = inspect(conn)
    for model in (WritingDailyStat, WritingGoal, WritingStatsTracking):
        table = model.__table__
        columns = inspector.get_columns(table.name)
        assert set(table.columns.keys()) == {column["name"] for column in columns}
        for column in columns:
            expected = table.c[column["name"]]
            assert column["nullable"] == expected.nullable
            assert column["type"].compile(conn.dialect) == expected.type.compile(conn.dialect)
        assert inspector.get_pk_constraint(table.name)["constrained_columns"] == list(table.primary_key.columns.keys())
        assert {fk["referred_table"] for fk in inspector.get_foreign_keys(table.name)} == {
            fk.column.table.name for fk in table.foreign_keys
        }
        assert all(fk["options"]["ondelete"] == "CASCADE" for fk in inspector.get_foreign_keys(table.name))
        assert {ck["name"] for ck in inspector.get_check_constraints(table.name)} == {
            ck.name for ck in table.constraints if isinstance(ck, CheckConstraint)
        }


async def test_writing_stats_old_book_and_guarded_downgrade(migration_db):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0070")
    async with engine.begin() as conn:
        await conn.execute(User.__table__.insert().values(id=1, username="legacy", password_hash="synthetic"))
        await conn.execute(Project.__table__.insert().values(id=1, user_id=1, title="旧书"))
        await conn.execute(Chapter.__table__.insert().values(
            project_id=1, title="旧章", chapter_no=1, content="甲" * 80_000, word_count=80_000,
        ))
    await asyncio.to_thread(command.upgrade, cfg, "0071")
    async with engine.begin() as conn:
        await conn.run_sync(_assert_schema)
        assert await conn.scalar(select(WritingStatsTracking.started_at)) is not None
        assert await conn.scalar(select(func.count()).select_from(WritingDailyStat)) == 0
        assert await conn.scalar(select(Chapter.word_count)) == 80_000
    for model, values in [(WritingDailyStat, {"day": date(2026, 1, 1), "manual_words": 10}), (WritingGoal, {})]:
        async with engine.begin() as conn:
            await conn.execute(model.__table__.insert().values(user_id=1, project_id=1, **values))
        with pytest.raises(RuntimeError, match="请先备份"):
            await asyncio.to_thread(command.downgrade, cfg, "0070")
        async with engine.begin() as conn:
            assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0071"
            assert await conn.scalar(select(func.count()).select_from(model)) == 1
            await conn.execute(model.__table__.delete())
    await asyncio.to_thread(command.downgrade, cfg, "0070")
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0070"
        assert not await conn.run_sync(lambda c: inspect(c).has_table("writing_daily_stats"))


@pytest.mark.parametrize("source_year", [2000, 2099])
async def test_writing_stats_single_book_import_starts_empty(client, app, monkeypatch, source_year):
    from app.services import writing_stats_overview
    from app.services.project_lifecycle import as_utc
    from app.services.writing_stats import BEIJING
    await register(client)
    source = await create_project(client)
    async with app.state.sessionmaker.begin() as db:
        await db.execute(update(Project).where(Project.id == source["id"]).values(
            created_at=datetime(source_year, 1, 1, tzinfo=UTC),
        ))
        await db.execute(update(WritingStatsTracking).values(started_at=datetime(1990, 1, 1, tzinfo=UTC)))
        user_id = await db.scalar(select(Project.user_id).where(Project.id == source["id"]))
        db.add(WritingDailyStat(user_id=user_id, project_id=source["id"], day=date(2026, 1, 1), manual_words=50))
        db.add(WritingGoal(user_id=user_id, project_id=source["id"], period="day", target_words=1000))
        assert await db.scalar(select(WritingStatsTracking.started_at)) is not None
    backup = await _backup(client, source["id"])
    assert not {"writing_goals", "writing_daily_stats", "writing_stats_tracking"}.intersection(backup["tables"])
    assert "writing_stats_started_at" not in backup["project"]
    before = datetime.now(UTC)
    copy_id = (await _import(client, backup))["project"]["id"]
    async with app.state.sessionmaker() as db:
        copy = await db.get(Project, copy_id)
        assert copy.created_at.year == source_year
        imported_at = as_utc(copy.writing_stats_started_at)
        assert before <= imported_at <= datetime.now(UTC)
        for model in (WritingDailyStat, WritingGoal):
            assert await db.scalar(select(func.count()).select_from(model).where(model.project_id == copy_id)) == 0
            assert await db.scalar(select(func.count()).select_from(model).where(model.project_id == source["id"])) == 1
    import_day = imported_at.astimezone(BEIJING).date()
    monkeypatch.setattr(writing_stats_overview, "_now", lambda: imported_at + timedelta(days=1))
    response = await client.get(f"/api/v1/projects/{copy_id}/writing-stats/overview", params={
        "start_date": (import_day - timedelta(days=1)).isoformat(),
    })
    assert response.status_code == 200, response.text
    days = response.json()["days"]
    assert [(row["manual_words"], row["partial"]) for row in days] == [(None, False), (0, True), (0, True)]
