from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.ai.providers.usage import ModelUsage
from app.core.database import create_engine, create_sessionmaker
from app.models import AIConfig, AiUsageCall, AiUsageTracking, Base, Chapter, Project, Task, User
from app.services import ai_usage_recording
from app.services import ai_usage_storage as usage
from app.services.ai_usage_storage import UsageCallIdentity, UsageMeasurement

NOW = datetime(2026, 11, 30, 15, 59, 59, tzinfo=UTC)


@pytest.fixture
async def storage_sessionmaker(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield create_sessionmaker(engine)
    finally:
        await engine.dispose()


async def _seed(db):
    author = User(username="author", password_hash="hash")
    editor = User(username="editor", password_hash="hash", created_at=NOW)
    db.add_all((author, editor))
    await db.flush()
    project = Project(user_id=author.id, title="作品")
    config = AIConfig(
        user_id=author.id, name="模型", provider="openai", model="fake", model_type="text", api_key_enc="unused",
    )
    db.add_all((project, config))
    await db.flush()
    chapter = Chapter(project_id=project.id, chapter_no=1, title="第一章")
    task = Task(user_id=editor.id, project_id=project.id, kind="chapter.generate", input={})
    db.add_all((chapter, task))
    await db.flush()
    identity = UsageCallIdentity(
        actor_id=editor.id, payer_id=author.id, project_id=project.id, chapter_id=chapter.id,
        task_id=task.id, task_key=task.statistics_key, config_instance_key=config.instance_key,
        provider=config.provider, model=config.model, model_type=config.model_type, kind=task.kind,
        actor_created_at=editor.created_at, payer_created_at=author.created_at, chapter_created_at=chapter.created_at,
    )
    return identity, config


async def test_price_snapshot_and_terminal_state_are_idempotent(storage_sessionmaker):
    own_id, edit_id = str(uuid4()), str(uuid4())
    sessionmaker = storage_sessionmaker
    async with sessionmaker() as db:
        identity, config = await _seed(db)
        assert await db.get(AiUsageTracking, 1) is not None
        old_price = usage.amount_to_nano("0.123456789")
        await usage.set_price(db, config, currency="CNY", input_price_nano=old_price, output_price_nano=0)
        await usage.reserve_call(db, identity, call_id=own_id, started_at=NOW)

        await usage.set_price(db, config, currency="USD", input_price_nano=usage.amount_to_nano("1"))
        await usage.reserve_call(db, identity, call_id=own_id, started_at=NOW + timedelta(minutes=1))
        collaborator = replace(
            identity, payer_id=identity.actor_id, payer_created_at=identity.actor_created_at, config_instance_key=None,
        )
        await usage.reserve_call(db, collaborator, call_id=edit_id, started_at=NOW)
        await db.commit()

    measurement = UsageMeasurement(
        input_tokens=1_000_000, input_source="provider", output_tokens=0, output_source="provider",
    )
    async with sessionmaker() as db:
        assert await usage.mark_call_sent(db, own_id)
        assert not await usage.mark_call_sent(db, own_id)
        assert await usage.finish_call(db, own_id, status="cancelled", sent=None, measurement=measurement,
                                       finished_at=NOW + timedelta(seconds=1))
        assert not await usage.finish_call(db, own_id, status="succeeded", sent=True, measurement=UsageMeasurement(),
                                           finished_at=NOW + timedelta(seconds=2))
        assert await usage.finish_call(db, edit_id, status="succeeded", sent=True, measurement=measurement,
                                       finished_at=NOW)
        await db.commit()

    async with sessionmaker() as db:
        call = await db.get(AiUsageCall, own_id)
        assert call.sent and call.status == "cancelled"
        assert call.author_paid and call.had_project
        assert (call.actor_id, call.payer_id, call.chapter_id, call.task_id, call.task_key) == (
            identity.actor_id, identity.payer_id, identity.chapter_id, identity.task_id, identity.task_key,
        )
        assert (call.currency, call.amount_nano, call.pricing_status) == ("CNY", old_price, "priced")
        assert call.started_at.replace(tzinfo=UTC) == NOW
        assert call.output_tokens == 0 and call.output_source == "provider"
        edit = await db.get(AiUsageCall, edit_id)
        assert not edit.author_paid and edit.payer_id == edit.actor_id
        assert edit.pricing_status == "missing_price"


async def test_unsent_missing_and_interrupted_are_explicit(storage_sessionmaker):
    missing, denied, interrupted, confirmed = (str(uuid4()) for _ in range(4))
    async with storage_sessionmaker() as db:
        identity, config = await _seed(db)
        await usage.set_price(db, config, currency="CNY", input_price_nano=0, output_price_nano=0)
        for call_id in (missing, denied, interrupted, confirmed):
            await usage.reserve_call(db, identity, call_id=call_id, started_at=NOW)

        untrusted = UsageMeasurement(input_tokens=True, input_source="provider", output_tokens=-10,
                                     output_source="provider", image_count=10**100, image_source="estimated")
        await usage.finish_call(db, missing, status="failed", sent=True, measurement=untrusted, finished_at=NOW)
        measured = UsageMeasurement(input_tokens=20, output_tokens=5, input_source="provider", output_source="provider")
        await usage.finish_call(db, denied, status="failed", sent=False, measurement=measured, finished_at=NOW)
        await usage.mark_call_sent(db, confirmed)
        later = NOW + timedelta(minutes=10)
        assert await usage.interrupt_stale_calls(db, before=later, finished_at=later) == 2
        await db.commit()

    async with storage_sessionmaker() as db:
        call = await db.get(AiUsageCall, missing)
        assert call.pricing_status == "missing_usage"
        assert (call.input_source, call.output_source, call.image_source) == ("unknown", "unknown", "unknown")
        call = await db.get(AiUsageCall, denied)
        assert call.status == call.pricing_status == "not_sent" and not call.sent and call.input_tokens is None
        call = await db.get(AiUsageCall, interrupted)
        assert call.status == "interrupted" and call.pricing_status == "unconfirmed" and call.sent is None
        call = await db.get(AiUsageCall, confirmed)
        assert call.status == "interrupted" and call.pricing_status == "missing_usage" and call.sent is True


async def test_project_and_model_deletion_preserve_account_cost(storage_sessionmaker):
    call_id = str(uuid4())
    async with storage_sessionmaker() as db:
        identity, config = await _seed(db)
        await usage.set_price(db, config, currency="CNY", input_price_nano=1_000_000, output_price_nano=0)
        await usage.set_budget(db, project_id=identity.project_id, currency="CNY", limit_nano=300, threshold_percent=80)
        account = await usage.set_budget(db, user_id=identity.payer_id, currency="CNY", limit_nano=300,
                                         threshold_percent=80)
        updated = await usage.set_budget(db, user_id=identity.payer_id, currency="CNY", limit_nano=200,
                                         threshold_percent=60)
        assert updated.id == account.id and updated.limit_nano == 200
        await usage.reserve_call(db, identity, call_id=call_id, started_at=NOW)
        measurement = UsageMeasurement(
            input_tokens=1, output_tokens=0, input_source="provider", output_source="provider",
        )
        await usage.finish_call(db, call_id, status="succeeded", sent=True, measurement=measurement, finished_at=NOW)
        await db.execute(delete(Project).where(Project.id == identity.project_id))
        await db.execute(delete(AIConfig).where(AIConfig.id == config.id))
        await db.commit()

    async with storage_sessionmaker() as db:
        call = await db.get(AiUsageCall, call_id)
        assert call.had_project and call.author_paid
        assert (call.project_id, call.chapter_id, call.task_id) == (None, None, None)
        assert call.task_key == identity.task_key
        assert call.payer_id == identity.payer_id and call.amount_nano == 1
        assert await usage.get_price(db, config) is None
        assert await usage.get_budget(db, project_id=identity.project_id, currency="CNY") is None
        assert (await usage.get_budget(db, user_id=identity.payer_id, currency="CNY")).threshold_percent == 60
        await db.execute(delete(User).where(User.id == identity.payer_id))
        await db.commit()

    async with storage_sessionmaker() as db:
        call = await db.get(AiUsageCall, call_id)
        assert call.payer_id is None and call.amount_nano == 1


async def test_reservation_never_attaches_reused_ids(storage_sessionmaker):
    call_id = str(uuid4())
    async with storage_sessionmaker() as db:
        identity, _ = await _seed(db)
        await db.commit()

    async with storage_sessionmaker() as db:
        await db.execute(delete(User).where(User.id == identity.actor_id))
        await db.execute(delete(Chapter).where(Chapter.id == identity.chapter_id))
        db.add(User(id=identity.actor_id, username="other", password_hash="hash",
                    created_at=NOW + timedelta(seconds=1)))
        await db.flush()
        db.add_all((Chapter(id=identity.chapter_id, project_id=identity.project_id, chapter_no=1, title="新章",
                            created_at=NOW + timedelta(minutes=1)),
                    Task(id=identity.task_id, user_id=identity.actor_id, project_id=identity.project_id,
                         kind=identity.kind, input={})))
        await db.flush()
        await usage.reserve_call(db, identity, call_id=call_id, started_at=NOW, price=None)
        await usage.reserve_call(db, identity, call_id=call_id, started_at=NOW, price=None)
        assert await db.scalar(select(func.count()).select_from(AiUsageCall)) == 1
        call = await db.get(AiUsageCall, call_id)
        assert (call.actor_id, call.chapter_id, call.task_id) == (None, None, None)
        assert call.payer_id == identity.payer_id and call.project_id == identity.project_id
        assert call.author_paid


def test_fixed_point_and_beijing_new_year():
    assert usage.nano_to_amount(usage.amount_to_nano("1000000000")) == "1000000000"
    assert usage.nano_to_amount(usage.amount_to_nano("0.000000001")) == "0.000000001"
    for invalid in ("-0.1", "0.0000000001", "1e2", "NaN", "1000000001"):
        with pytest.raises(ValueError):
            usage.amount_to_nano(invalid)
    measurement = UsageMeasurement(input_tokens=1, output_tokens=1)
    assert usage.estimate_amount_nano("text", measurement, input_price_nano=500_000, output_price_nano=500_000) == 1
    assert usage.estimate_amount_nano("image", UsageMeasurement(image_count=0), image_price_nano=5) == 0
    last = datetime(2026, 12, 31, 15, 59, 59, tzinfo=UTC)
    boundary = last + timedelta(seconds=1)
    assert usage.monthly_budget_window(last)[1] == boundary
    assert usage.monthly_budget_window(boundary)[0] == boundary


@pytest.mark.parametrize(("cached_tokens", "cached_rate", "expected"), [
    (1, 0, 2), (1, 500_000, 2), (1, None, 3), (0, 0, 3),
    (None, 0, 3), (True, 0, 3), (1.0, 0, 3), (-1, 0, 3), (3, 0, 3),
])
def test_cached_input_estimate_rounds_once_and_ignores_invalid_counts(cached_tokens, cached_rate, expected):
    measurement = UsageMeasurement(input_tokens=2, output_tokens=1, cached_input_tokens=cached_tokens)
    assert usage.estimate_amount_nano(
        "text", measurement, input_price_nano=1_000_000, output_price_nano=500_000,
        cached_input_price_nano=cached_rate,
    ) == expected
    split = UsageMeasurement(input_tokens=2, cached_input_tokens=1, output_tokens=0)
    assert usage.estimate_amount_nano(
        "text", split, input_price_nano=400_000, cached_input_price_nano=400_000,
    ) == 1


@pytest.mark.parametrize(("cached", "input_source", "stored", "amount"), [
    (0, "provider", 0, 10), (25_000, "provider", 25_000, 8),
    (None, "provider", None, 10), (100_001, "provider", None, 10),
    (25_000, "unknown", None, None),
])
async def test_fixed_cached_rate_is_frozen_and_valid_usage_keeps_total(
    storage_sessionmaker, cached, input_source, stored, amount,
):
    async with storage_sessionmaker() as db:
        identity, config = await _seed(db)
        await usage.set_price(db, config, currency="CNY", input_price_nano=100, cached_input_price_nano=0)
        call_id = str(uuid4())
        await usage.reserve_call(db, identity, call_id=call_id, started_at=NOW)
        await usage.set_price(db, config, currency="USD", input_price_nano=500, cached_input_price_nano=300)
        await usage.finish_call(
            db, call_id, status="succeeded", sent=True, finished_at=NOW,
            measurement=UsageMeasurement(
                input_tokens=100_000, input_source=input_source, cached_input_tokens=cached,
                output_tokens=0, output_source="provider",
            ),
        )
        row = await db.get(AiUsageCall, call_id)
        assert (row.input_tokens, row.cached_input_tokens) == (100_000 if input_source == "provider" else None, stored)
        assert (row.currency, row.price_period, row.price_cached_input_nano, row.amount_nano) == (
            "CNY", "fixed", 0, amount,
        )


async def test_schedule_without_cached_rates_keeps_the_0062_payload(storage_sessionmaker):
    async with storage_sessionmaker() as db:
        identity, config = await _seed(db)
        call_id = str(uuid4())
        rule = usage.OffPeakPrice(start="23:00", end="07:00", input_price_nano=400, output_price_nano=0)
        price = await usage.set_price(db, config, currency="USD", input_price_nano=900, off_peak=rule)
        await usage.reserve_call(db, identity, call_id=call_id, started_at=NOW)
        row = await db.get(AiUsageCall, call_id)
        assert price.off_peak == {
            "start": "23:00", "end": "07:00", "timezone": "Asia/Shanghai", "input_price_nano": 400,
            "output_price_nano": 0, "image_price_nano": None,
        }
        assert row.pending_price_schedule == {
            "currency": "USD", "input_price_nano": 900, "output_price_nano": None, "image_price_nano": None,
            "off_peak": price.off_peak,
        }
        await usage.finish_call(
            db, call_id, status="succeeded", sent=True, finished_at=NOW,
            measurement=UsageMeasurement(
                input_tokens=1_000_000, input_source="provider", cached_input_tokens=200_000,
                output_tokens=0, output_source="provider",
            ),
        )
        await db.refresh(row)
        assert (row.price_period, row.price_cached_input_nano, row.cached_input_tokens, row.amount_nano) == (
            "off_peak", None, 200_000, 400,
        )


@pytest.mark.parametrize(("model_type", "rate", "off_peak"), [
    ("text", -1, False), ("text", True, True), ("text", usage.MAX_RATE_NANO + 1, True),
    ("embedding", 0, False), ("image", 0, True),
])
async def test_cached_input_rate_respects_model_type_and_range(storage_sessionmaker, model_type, rate, off_peak):
    async with storage_sessionmaker() as db:
        _, config = await _seed(db)
        config.model_type = model_type
        rates = {"image_price_nano": 100} if model_type == "image" else {"input_price_nano": 100}
        with pytest.raises(ValueError):
            if off_peak:
                rule = usage.OffPeakPrice(start="23:00", end="07:00", cached_input_price_nano=rate, **rates)
                await usage.set_price(db, config, currency="CNY", off_peak=rule, **rates)
            else:
                await usage.set_price(db, config, currency="CNY", cached_input_price_nano=rate, **rates)


@pytest.mark.parametrize(("hour", "minute", "second", "period"), [
    (14, 59, 59, "peak"),
    (15, 0, 0, "off_peak"),
    (16, 0, 0, "off_peak"),
    (22, 59, 59, "off_peak"),
    (23, 0, 0, "peak"),
])
def test_price_period_at_beijing_boundaries(hour, minute, second, period):
    snapshot = usage.UsagePriceSnapshot(
        currency="CNY", input_price_nano=900, output_price_nano=1200,
        off_peak=usage.OffPeakPrice(start="23:00", end="07:00", input_price_nano=0, output_price_nano=200),
    )
    selected = usage.price_at(snapshot, datetime(2026, 11, 30, hour, minute, second, tzinfo=UTC))
    assert (selected.period, selected.timezone) == (period, "Asia/Shanghai")
    assert selected.input_price_nano == (0 if period == "off_peak" else 900)


def test_explicit_timezone_and_daytime_period():
    snapshot = usage.UsagePriceSnapshot(
        currency="USD", image_price_nano=100,
        off_peak=usage.OffPeakPrice(timezone="UTC", start="06:00", end="18:00", image_price_nano=0),
    )
    for hour, period, rate in [(5, "peak", 100), (6, "off_peak", 0), (18, "peak", 100)]:
        chosen = usage.price_at(snapshot, datetime(2026, 11, 30, hour, tzinfo=UTC))
        assert (chosen.period, chosen.image_price_nano, chosen.timezone) == (period, rate, "UTC")


def test_additional_daily_windows_cover_boundaries_and_repeat():
    snapshot = usage.UsagePriceSnapshot(
        currency="USD", image_price_nano=100,
        off_peak=usage.OffPeakPrice(
            timezone="UTC", start="12:00", end="14:00", image_price_nano=5,
            additional_windows=(
                usage.OffPeakWindow(start="22:00", end="02:00"),
                usage.OffPeakWindow(start="14:00", end="15:00"),
            ),
        ),
    )
    for date, peak_hours, off_peak_hours in (
        ("2026-11-30", ("11:59", "15:00", "21:59"), ("12:00", "14:00", "14:59", "22:00", "23:59")),
        ("2026-12-01", ("02:00", "15:00"), ("00:00", "01:59", "12:00", "22:00")),
    ):
        for period, hours, price in (("peak", peak_hours, 100), ("off_peak", off_peak_hours, 5)):
            for clock in hours:
                timestamp = datetime.fromisoformat(f"{date}T{clock}:00+00:00")
                selected = usage.price_at(snapshot, timestamp)
                assert (selected.period, selected.image_price_nano) == (period, price)


def test_adjacent_windows_can_touch_at_midnight():
    snapshot = usage.UsagePriceSnapshot(
        currency="CNY", image_price_nano=100,
        off_peak=usage.OffPeakPrice(
            timezone="UTC", start="23:00", end="00:00", image_price_nano=0,
            additional_windows=(usage.OffPeakWindow(start="00:00", end="04:00"),),
        ),
    )
    assert usage.price_at(snapshot, datetime(2026, 12, 1, 0, 0, tzinfo=UTC)).period == "off_peak"
    assert usage.price_at(snapshot, datetime(2026, 12, 1, 4, 0, tzinfo=UTC)).period == "peak"


@pytest.mark.parametrize("changes", [
    {"start": "24:00"}, {"start": "7:00"}, {"end": "23:00"}, {"timezone": "No/Such_Zone"},
    {"timezone": "localtime"}, {"timezone": "right/UTC"},
])
def test_invalid_off_peak_rule_is_rejected(changes):
    with pytest.raises(ValueError):
        usage.OffPeakPrice(**({"start": "23:00", "end": "07:00", "input_price_nano": 0} | changes))


async def test_recorder_prices_each_dispatch_from_its_frozen_rule(storage_sessionmaker):
    edge = datetime(2026, 11, 30, 15, tzinfo=UTC)  # 北京时间 23:00
    initial, retry, later = (str(uuid4()) for _ in range(3))
    async with storage_sessionmaker() as db:
        identity, config = await _seed(db)
        await usage.set_price(
            db, config, currency="CNY", input_price_nano=900, output_price_nano=0, cached_input_price_nano=500,
            off_peak=usage.OffPeakPrice(
                start="12:00", end="14:00", input_price_nano=100, output_price_nano=0, cached_input_price_nano=0,
                additional_windows=(usage.OffPeakWindow(start="23:00", end="07:00"),),
            ),
        )
        await db.commit()

    recorder = ai_usage_recording.UsageRecorder(ai_usage_recording.UsageBinding(identity, storage_sessionmaker))
    before = edge - timedelta(seconds=1)
    await recorder.begin(initial, before - timedelta(seconds=1))
    await recorder.begin(retry, before)
    async with storage_sessionmaker() as db:
        pending = await db.get(AiUsageCall, retry)
        assert pending.pending_price_schedule["off_peak"]["additional_windows"] == [
            {"start": "23:00", "end": "07:00"},
        ]
        assert pending.price_period is None and pending.price_input_nano is None
        await usage.set_price(db, config, currency="CNY", input_price_nano=250, output_price_nano=0)
        await db.commit()
    await recorder.begin(later, edge + timedelta(seconds=1))

    for call_id, started, dispatch, status in (
        (initial, before - timedelta(seconds=1), before, "failed"),
        (retry, before, edge, "completed"),
        (later, edge + timedelta(seconds=1), None, "completed"),
    ):
        await recorder.finish(ModelUsage(
            call_id, started, status, True, 1_000_000, 0, None, dispatch, cached_input_tokens=500_000,
        ))
    await recorder.flush()

    async with storage_sessionmaker() as db:
        rows = [await db.get(AiUsageCall, call_id) for call_id in (initial, retry, later)]
        assert [
            (row.price_period, row.price_input_nano, row.price_cached_input_nano, row.amount_nano) for row in rows
        ] == [
            ("peak", 900, 500, 700), ("off_peak", 100, 0, 50), ("fixed", 250, None, 250),
        ]
        assert all(row.input_tokens == 1_000_000 and row.cached_input_tokens == 500_000 for row in rows)
        assert all(row.pending_price_schedule is None for row in rows)
        assert [row.started_at.replace(tzinfo=UTC) for row in rows] == [before, edge, edge + timedelta(seconds=1)]


async def test_video_charge_is_frozen_once_and_budgets_follow_payer(storage_sessionmaker):
    from app.core.errors import BadRequestError
    from app.schemas.ai_usage import UsageFilters
    from app.services import ai_media_billing, ai_usage_query, authorization_service

    await authorization_service.initialize(storage_sessionmaker)
    async with storage_sessionmaker() as db:
        identity, cfg = await _seed(db)
        cfg.model_type = "video"
        cfg.provider = "openai_compat"
        identity = replace(identity, provider=cfg.provider, model_type="video", author_paid=True)
        unit = usage.amount_to_nano("1")
        await usage.set_price(db, cfg, currency="CNY", video_price_nano=unit, off_peak=usage.OffPeakPrice(
            start="22:00", end="07:00", video_price_nano=unit // 2,
        ))
        await usage.set_budget(db, user_id=identity.payer_id, currency="CNY", limit_nano=4 * unit, threshold_percent=90)
        await usage.set_budget(db, project_id=identity.project_id, currency="CNY", limit_nano=2 * unit,
                               threshold_percent=90)
        with pytest.raises(BadRequestError, match="预算"):
            await ai_media_billing.check_video_budget(db, identity, seconds=5, now=NOW)
        personal = replace(identity, project_id=None, author_paid=False)
        with pytest.raises(BadRequestError, match="预算"):
            await ai_media_billing.check_video_budget(db, personal, seconds=9, now=NOW)
        frozen = await ai_media_billing.check_video_budget(db, identity, seconds=4, now=NOW)
        await usage.set_price(db, cfg, currency="USD", video_price_nano=2 * unit)
        with pytest.raises(BadRequestError, match="预算"):
            await ai_media_billing.check_frozen_video_budget(
                db, identity, seconds=5, now=NOW + timedelta(hours=12), price=frozen,
            )
        await ai_media_billing.check_frozen_video_budget(
            db, identity, seconds=4, now=NOW + timedelta(hours=12), price=frozen,
        )
        job_id = str(uuid4())
        for _ in range(2):
            await ai_media_billing.settle_video(
                db, identity, job_id=job_id, started_at=NOW, finished_at=NOW + timedelta(minutes=1),
                seconds=4, price=frozen,
            )
        owner = await ai_usage_query.list_usage(db, UsageFilters(), actor_id=identity.payer_id)
        project = await ai_usage_query.list_usage(
            db, UsageFilters(), actor_id=identity.payer_id, project_id=identity.project_id,
        )
        assert project.total == owner.total == 1 and project.summary.video_seconds == owner.summary.video_seconds == 4
        assert owner.items[0].per_video_second == "0.5"
        assert owner.items[0].price_period == "off_peak"
        assert [(cost.currency, cost.amount) for cost in owner.summary.costs] == [("CNY", "2")]
        editor = await ai_usage_query.list_usage(db, UsageFilters(), actor_id=identity.actor_id)
        assert editor.items[0].per_video_second is None and not editor.summary.costs

        await usage.delete_price(db, cfg)
        no_price = await ai_media_billing.check_video_budget(db, identity, seconds=4, now=NOW)
        free_id = await ai_media_billing.settle_video(
            db, identity, job_id=str(uuid4()), started_at=NOW, finished_at=NOW, seconds=4, price=no_price,
        )
        free = await db.get(AiUsageCall, free_id)
        assert (free.video_seconds, free.amount_nano, free.pricing_status) == (4, None, "missing_price")
