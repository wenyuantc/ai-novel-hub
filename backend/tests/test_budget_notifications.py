"""预算提醒在真实调用收尾事务上验收，迁移、并发也使用相同的收尾入口。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import AiUsageCall, Notification, Project, User
from app.models.ai_usage import AiBudgetPeriod
from app.services import ai_usage_storage as usage
from app.services import authorization_service
from app.services import notification_service as inbox
from tests.test_ai_usage_storage import _seed
from tests.test_ai_usage_storage import storage_sessionmaker as storage_sessionmaker
from tests.test_task_worker_migration import migration_db as migration_db

MONTH_END = datetime(2026, 11, 30, 15, 59, 59, 999999, tzinfo=UTC)
NEXT_MONTH = MONTH_END + timedelta(microseconds=1)
_MEASURE = usage.UsageMeasurement(image_count=1, image_source="provider")


async def _reserve(db, actor, payer, project, amount, *, started=MONTH_END, currency="CNY"):
    call_id = str(uuid4())
    await usage.reserve_call(
        db, usage.UsageCallIdentity(actor_id=actor, payer_id=payer, project_id=project,
                                   provider="fake", model="test", model_type="image", kind="cover"),
        price=usage.UsagePriceSnapshot(currency=currency, image_price_nano=amount), call_id=call_id, started_at=started,
    )
    return call_id


async def _settle(db, call_id):
    return await usage.finish_call(db, call_id, status="succeeded", sent=True, measurement=_MEASURE,
                                   finished_at=NEXT_MONTH)


async def test_account_and_author_paid_book_notifications(storage_sessionmaker):
    await authorization_service.initialize(storage_sessionmaker)
    async with storage_sessionmaker() as db:
        identity, _ = await _seed(db)
        payer, editor, project = identity.payer_id, identity.actor_id, identity.project_id
        for key in ({"user_id": payer}, {"project_id": project}):
            await usage.set_budget(db, currency="CNY", limit_nano=100, threshold_percent=80, **key)
        for amount, unread in [(79, 0), (1, 2), (5, 2), (15, 4)]:
            call = await _reserve(db, editor, payer, project, amount)
            assert await _settle(db, call)
            assert not await _settle(db, call)
            assert await inbox.unread_count(db, payer) == unread
        assert await inbox.unread_count(db, editor) == 0
        rows = (await db.scalars(select(Notification).order_by(Notification.id))).all()
        assert [(r.variant, r.project_id) for r in rows] == [
            ("near", None), ("near", project), ("exceeded", None), ("exceeded", project),
        ]
        target = await inbox.mark_read(db, payer, rows[1].public_id, open_target=True)
        assert target.project_id == project and target.currency == "CNY"

        # 北京时间新月直接跨两档；作品累计已达额度，只产生个人已达额度提醒。
        await _settle(db, await _reserve(db, editor, payer, project, 105, started=NEXT_MONTH))
        last = await db.scalar(select(Notification).order_by(Notification.id.desc()).limit(1))
        assert last.variant == "exceeded" and last.project_id is None and "2026-12" in last.event_key
        assert await inbox.unread_count(db, payer) == 4


async def test_budget_changes_baseline_preferences_and_rollback(storage_sessionmaker):
    await authorization_service.initialize(storage_sessionmaker)
    async with storage_sessionmaker() as db:
        identity, _ = await _seed(db)
        payer = identity.payer_id
        settings = dict(user_id=payer, currency="CNY", limit_nano=100, threshold_percent=80)
        budget = await usage.set_budget(db, **settings)
        key = budget.instance_key
        await inbox.set_preference(db, payer, "budget", False)
        await _settle(db, await _reserve(db, payer, payer, None, 85))
        await inbox.set_preference(db, payer, "budget", True)
        assert await inbox.unread_count(db, payer) == 0
        assert (await usage.set_budget(db, **settings)).instance_key == key
        await _settle(db, await _reserve(db, payer, payer, None, 10))
        assert await inbox.unread_count(db, payer) == 0
        call = await _reserve(db, payer, payer, None, 5)
        await db.commit()
        await _settle(db, call)
        assert await inbox.unread_count(db, payer) == 1
        await db.rollback()
        assert await inbox.unread_count(db, payer) == 0
        assert await _settle(db, call)
        assert await inbox.unread_count(db, payer) == 1

        # 实质修改的首笔入账，历史花费已经达到 50%，只在新额度达到时产生一条。
        budget = await usage.set_budget(db, **(settings | {"limit_nano": 200, "threshold_percent": 50}))
        assert budget.instance_key != key
        await _settle(db, await _reserve(db, payer, payer, None, 1))
        assert await inbox.unread_count(db, payer) == 0
        await _settle(db, await _reserve(db, payer, payer, None, 99))
        assert await inbox.unread_count(db, payer) == 1
        old_key = budget.instance_key
        await usage.delete_budget(db, user_id=payer, currency="CNY")
        assert await inbox.unread_count(db, payer) == 0
        recreated = await usage.set_budget(db, **(settings | {"limit_nano": 250, "threshold_percent": 100}))
        assert recreated.instance_key != old_key
        await _settle(db, await _reserve(db, payer, payer, None, 50))
        assert await inbox.unread_count(db, payer) == 1


async def test_legacy_baseline_and_concurrent_crossing(migration_db):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0072")
    async with engine.begin() as conn:
        await conn.execute(User.__table__.insert().values(id=1, username="old", password_hash="test"))
        await conn.execute(Project.__table__.insert().values(id=1, user_id=1, title="有历史费用"))
        for columns in ("user_id", "project_id"):
            await conn.execute(text(f"INSERT INTO ai_usage_budgets({columns},currency,limit_nano,threshold_percent) "
                                    "VALUES(1,'CNY',100,80)"))
        await conn.execute(AiUsageCall.__table__.insert().values(
            id=str(uuid4()), payer_id=1, actor_id=1, project_id=1, had_project=True, author_paid=True,
            provider="fake", model="test", model_type="image", kind="cover", started_at=MONTH_END,
            finished_at=MONTH_END, status="succeeded", pricing_status="priced", sent=True,
            currency="CNY", amount_nano=79,
        ))
    await asyncio.to_thread(command.upgrade, cfg, "0073")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as db:
        rows = (await db.scalars(select(AiBudgetPeriod))).all()
        assert {(r.period, r.spent_nano) for r in rows} == {("all", 79), ("2026-11", 79)}
        assert await db.scalar(select(func.count()).select_from(Notification)) == 0

    # 先验证 0073 当时的历史回填，再让现行计费服务在实际部署版本并发入账。
    await asyncio.to_thread(command.upgrade, cfg, "head")
    async with factory.begin() as db:
        assert await inbox.unread_count(db, 1) == 0
        assert await _settle(db, await _reserve(db, 1, 1, 1, 1))
        assert await inbox.unread_count(db, 1) == 2
        pending = [await _reserve(db, 1, 1, 1, 10) for _ in range(2)]

    async def settle_once(call_id):
        async with factory.begin() as db:
            return await _settle(db, call_id)

    assert all(await asyncio.gather(*(settle_once(call) for call in pending)))
    assert not await settle_once(pending[-1])
    async with factory() as db:
        assert await inbox.unread_count(db, 1) == 4
        assert await db.scalar(select(func.count()).select_from(Notification).where(
            Notification.variant == "exceeded")) == 2
    await asyncio.to_thread(command.downgrade, cfg, "0073")
    with pytest.raises(RuntimeError, match="存在预算通知或周期进度，请先备份"):
        await asyncio.to_thread(command.downgrade, cfg, "0072")


@pytest.mark.parametrize("project_budget", [False, True])
async def test_budget_link_cannot_read_change_or_remove_replacement(client, make_client, project_budget):
    from tests.helpers import create_project, register

    await register(client)
    guest = make_client()
    await register(guest, "budget-guest")
    path = "/api/v1/ai-usage/budgets"
    if project_budget:
        project = await create_project(client)
        path = f"/api/v1/projects/{project['id']}/ai-usage/budgets"
    body = {"limit": "100", "threshold_percent": 80}
    original = await client.put(f"{path}/CNY", json=body)
    assert original.status_code == 200
    link = {"budget_key": original.json()["instance_key"]}
    assert (await client.get(path, params=link)).json() == [original.json()]
    assert (await guest.get(path, params=link)).status_code == 404
    assert (await client.put(f"{path}/USD", params=link, json=body)).status_code == 404
    remove = {"content": "null", "headers": {"Content-Type": "application/json"}}
    assert (await client.put(f"{path}/CNY", params=link, **remove)).status_code == 200
    replacement = (await client.put(f"{path}/CNY", json=body)).json()
    assert replacement["instance_key"] != link["budget_key"]
    assert (await client.get(path, params=link)).status_code == 404
    assert (await client.put(f"{path}/CNY", params=link, json=body | {"limit": "10"})).status_code == 404
    assert (await client.put(f"{path}/CNY", params=link, **remove)).status_code == 404
    assert (await client.get(path)).json() == [replacement]
