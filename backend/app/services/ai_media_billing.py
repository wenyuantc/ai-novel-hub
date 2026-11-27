"""图片、视频发起前的预算预检，以及外部视频成功时的单事务计量。"""

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.models.ai_usage import MAX_VIDEO_SECONDS, NANOS_PER_UNIT
from app.services import ai_usage_storage as storage
from app.services.ai_usage_query import budget_progress


async def _current_price(
    db: AsyncSession, identity: storage.UsageCallIdentity, now: datetime,
) -> storage.EffectiveUsagePrice | None:
    snapshot = await storage.price_snapshot(db, identity)
    return storage.price_at(snapshot, now) if snapshot is not None else None


async def _check_budget(
    db: AsyncSession, identity: storage.UsageCallIdentity, measurement: storage.UsageMeasurement,
    now: datetime, price: storage.EffectiveUsagePrice | None,
) -> None:
    if price is None or identity.payer_id is None:
        return
    estimated = storage.estimate_amount_nano(
        identity.model_type, measurement, image_price_nano=price.image_price_nano,
        video_price_nano=price.video_price_nano,
    )
    if estimated is None:
        return
    budgets = await budget_progress(db, identity.payer_id, now=now)
    if identity.author_paid and identity.project_id is not None:
        budgets += await budget_progress(db, identity.payer_id, project_id=identity.project_id, now=now)
    estimate = Decimal(estimated) / NANOS_PER_UNIT
    for budget in budgets:
        if budget.currency == price.currency and Decimal(budget.spent) + estimate > Decimal(budget.limit):
            raise BadRequestError("预计生成费用超过可用预算")


async def check_image_budget(db: AsyncSession, identity: storage.UsageCallIdentity, *, now: datetime) -> None:
    price = await _current_price(db, identity, now)
    await _check_budget(db, identity, storage.UsageMeasurement(image_count=1), now, price)


def _video_measurement(seconds: int) -> storage.UsageMeasurement:
    if type(seconds) is not int or not 1 <= seconds <= MAX_VIDEO_SECONDS:
        raise BadRequestError("视频时长无效")
    return storage.UsageMeasurement(video_seconds=seconds)


async def check_video_budget(
    db: AsyncSession, identity: storage.UsageCallIdentity, *, seconds: int, now: datetime,
) -> dict | None:
    """返回服务端冻结的本次报价，随台账保存，允许等待、跨档及重启后按原价结算。"""
    price = await _current_price(db, identity, now)
    await _check_budget(db, identity, _video_measurement(seconds), now, price)
    return asdict(price) if price is not None else None


async def check_frozen_video_budget(
    db: AsyncSession, identity: storage.UsageCallIdentity, *, seconds: int, now: datetime, price: dict | None,
) -> None:
    """在真正发送前复核当前余额；同一笔任务的币种、档位与单价以冻结报价为准。"""
    await _check_budget(
        db, identity, _video_measurement(seconds), now,
        storage.EffectiveUsagePrice(**price) if price is not None else None,
    )


async def settle_video(
    db: AsyncSession, identity: storage.UsageCallIdentity, *, job_id: str, seconds: int | None,
    started_at: datetime, finished_at: datetime, price: dict | None,
) -> str:
    """台账 UUID 同时是调用 UUID，重复收尾时保留原记录，调用者提交。"""
    await storage.reserve_call(
        db, identity, call_id=job_id, started_at=started_at,
        price=storage.EffectiveUsagePrice(**price) if price is not None else None, verify_identity=True,
    )
    await storage.finish_call(
        db, job_id, status="succeeded", sent=True, measurement=storage.UsageMeasurement(video_seconds=seconds),
        finished_at=finished_at,
    )
    return job_id
