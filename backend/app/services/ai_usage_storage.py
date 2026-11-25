"""调用用量、单价和预算的事务原语。提交由调用方负责。"""

import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import Literal, Protocol, Self
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import String, case, cast, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AIConfig, AiModelPrice, AiUsageBudget, AiUsageCall, Chapter, Project, Task, User
from app.models.ai_usage import (
    MAX_BUDGET_NANO,
    MAX_IMAGES,
    MAX_RATE_NANO,
    MAX_TOKENS,
    NANOS_PER_UNIT,
    PENDING_CALL_WHERE,
)
from app.services import ai_budget_notifications
from app.services.ai_usage_totals import monthly_budget_window as monthly_budget_window
from app.services.project_lifecycle import as_utc

CURRENCIES = ("CNY", "USD")
TOKENS_PER_PRICE_UNIT = 1_000_000
_AMOUNT = re.compile(r"[0-9]{1,10}(?:\.[0-9]{1,9})?\Z")
_LOCAL_MINUTE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
UsageSource = Literal["provider", "estimated", "unknown"]
TerminalStatus = Literal["succeeded", "failed", "cancelled", "interrupted", "not_sent"]
PricePeriod = Literal["fixed", "peak", "off_peak"]


class PriceConfig(Protocol):
    @property
    def instance_key(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def model_type(self) -> str: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageCallIdentity:
    actor_id: int | None
    payer_id: int | None
    provider: str
    model: str
    model_type: str
    kind: str
    project_id: int | None = None
    chapter_id: int | None = None
    task_id: int | None = None
    task_key: str | None = None
    config_instance_key: str | None = None
    author_paid: bool = False
    actor_created_at: datetime | None = None
    payer_created_at: datetime | None = None
    chapter_created_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageMeasurement:
    input_tokens: int | None = None
    output_tokens: int | None = None
    image_count: int | None = None
    input_source: UsageSource = "unknown"
    output_source: UsageSource = "unknown"
    image_source: UsageSource = "unknown"
    cached_input_tokens: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class OffPeakWindow:
    start: str
    end: str

    def __post_init__(self) -> None:
        if any(type(value) is not str or not _LOCAL_MINUTE.fullmatch(value) for value in (self.start, self.end)):
            raise ValueError("空闲时段需使用 HH:MM 格式")
        if self.start == self.end:
            raise ValueError("空闲时段的开始与结束时间不能相同")

    def minute_ranges(self) -> Iterator[tuple[str, str]]:
        if self.start < self.end:
            yield self.start, self.end
            return
        yield self.start, "24:00"
        if self.end != "00:00":
            yield "00:00", self.end


@dataclass(frozen=True, slots=True, kw_only=True)
class OffPeakPrice:
    start: str
    end: str
    timezone: str = "Asia/Shanghai"
    input_price_nano: int | None = None
    output_price_nano: int | None = None
    image_price_nano: int | None = None
    cached_input_price_nano: int | None = None
    additional_windows: tuple[OffPeakWindow, ...] = ()

    def __post_init__(self) -> None:
        ranges = sorted(self.minute_ranges())
        if any(start < previous_end for (_, previous_end), (start, _) in pairwise(ranges)):
            raise ValueError("空闲时段不能重叠")
        if (
            type(self.timezone) is not str or not 0 < len(self.timezone) <= 64
            or self.timezone in {"localtime", "posixrules"} or self.timezone.startswith(("posix/", "right/"))
        ):
            raise ValueError("空闲时段需使用有效的 IANA 时区")
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("空闲时段需使用有效的 IANA 时区") from None

    def minute_ranges(self) -> Iterator[tuple[str, str]]:
        yield from OffPeakWindow(start=self.start, end=self.end).minute_ranges()
        for window in self.additional_windows:
            yield from window.minute_ranges()

    @classmethod
    def from_payload(cls, payload: dict) -> Self:
        windows = tuple(OffPeakWindow(**window) for window in payload.get("additional_windows", ()))
        return cls(**(payload | {"additional_windows": windows}))


@dataclass(frozen=True, slots=True, kw_only=True)
class UsagePriceSnapshot:
    currency: str
    input_price_nano: int | None = None
    output_price_nano: int | None = None
    image_price_nano: int | None = None
    off_peak: OffPeakPrice | None = None
    cached_input_price_nano: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class EffectiveUsagePrice:
    currency: str
    period: PricePeriod
    timezone: str | None
    input_price_nano: int | None
    output_price_nano: int | None
    image_price_nano: int | None
    cached_input_price_nano: int | None = None


def model_price_snapshot(price: AiModelPrice) -> UsagePriceSnapshot:
    return UsagePriceSnapshot(
        currency=price.currency, input_price_nano=price.input_price_nano,
        output_price_nano=price.output_price_nano, image_price_nano=price.image_price_nano,
        off_peak=OffPeakPrice.from_payload(price.off_peak) if price.off_peak is not None else None,
        cached_input_price_nano=price.cached_input_price_nano,
    )


def _price_payload(price: OffPeakPrice | UsagePriceSnapshot) -> dict:
    # 省略尚未使用的新字段，旧单时段价表和待发送快照沿用原结构。
    return asdict(price, dict_factory=lambda fields: {
        key: value for key, value in fields
        if (key != "cached_input_price_nano" or value is not None) and (key != "additional_windows" or value)
    })


def price_at(snapshot: UsagePriceSnapshot, timestamp: datetime) -> EffectiveUsagePrice:
    """按报价时区的本地分钟选档，开始包含，结束排除。"""
    rule = snapshot.off_peak
    period: PricePeriod = "fixed"
    rates: UsagePriceSnapshot | OffPeakPrice = snapshot
    if rule is not None:
        minute = as_utc(timestamp).astimezone(ZoneInfo(rule.timezone)).strftime("%H:%M")
        off_peak = any(start <= minute < end for start, end in rule.minute_ranges())
        period = "off_peak" if off_peak else "peak"
        if off_peak:
            rates = rule
    return EffectiveUsagePrice(
        currency=snapshot.currency, period=period, timezone=rule.timezone if rule is not None else None,
        input_price_nano=rates.input_price_nano, output_price_nano=rates.output_price_nano,
        image_price_nano=rates.image_price_nano, cached_input_price_nano=rates.cached_input_price_nano,
    )


def amount_to_nano(value: str | Decimal) -> int:
    if isinstance(value, str) and _AMOUNT.fullmatch(value):
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 1_000_000_000:
        raise ValueError("金额须为非负十进制数，且不超过十亿")
    if value.as_tuple().exponent < -9:
        raise ValueError("金额最多支持 9 位小数")
    return int(value * NANOS_PER_UNIT)


def nano_to_amount(value: int | None) -> str | None:
    if value is None:
        return None
    whole, fractional = divmod(value, NANOS_PER_UNIT)
    return f"{whole}.{fractional:09d}".rstrip("0").rstrip(".") if fractional else str(whole)


def _reported(quantity: int | None, source: UsageSource, maximum: int) -> tuple[int | None, UsageSource]:
    if type(quantity) is int and 0 <= quantity <= maximum and source in ("provider", "estimated"):
        return quantity, source
    return None, "unknown"


def _reported_cached_input(cached: int | None, total: int | None) -> int | None:
    if type(cached) is int and type(total) is int and 0 <= cached <= total <= MAX_TOKENS:
        return cached
    return None


def _normalized(model_type: str, measurement: UsageMeasurement) -> UsageMeasurement:
    input_tokens, input_source = _reported(measurement.input_tokens, measurement.input_source, MAX_TOKENS)
    output_tokens, output_source = _reported(measurement.output_tokens, measurement.output_source, MAX_TOKENS)
    image_count, image_source = _reported(measurement.image_count, measurement.image_source, MAX_IMAGES)
    return UsageMeasurement(
        input_tokens=input_tokens, input_source=input_source,
        output_tokens=output_tokens, output_source=output_source,
        image_count=image_count, image_source=image_source,
        cached_input_tokens=(
            _reported_cached_input(measurement.cached_input_tokens, input_tokens) if model_type == "text" else None
        ),
    )


def _price_measurement(
    model_type: str, measurement: UsageMeasurement, *,
    input_price_nano: int | None, output_price_nano: int | None, image_price_nano: int | None,
    cached_input_price_nano: int | None,
) -> tuple[int | None, str]:
    if model_type == "image":
        components = ((measurement.image_count, image_price_nano),)
        denominator = 1
    elif model_type == "embedding":
        components = ((measurement.input_tokens, input_price_nano),)
        denominator = TOKENS_PER_PRICE_UNIT
    elif model_type == "text":
        total = measurement.input_tokens
        cached = _reported_cached_input(measurement.cached_input_tokens, total)
        input_components = ((total, input_price_nano),)
        if cached is not None and total is not None and cached_input_price_nano is not None:
            input_components = ((total - cached, input_price_nano), (cached, cached_input_price_nano))
        components = (*input_components, (measurement.output_tokens, output_price_nano))
        denominator = TOKENS_PER_PRICE_UNIT
    else:
        raise ValueError("模型类型无效")
    if all(rate is None for _, rate in components) or any(
        quantity != 0 and rate is None for quantity, rate in components
    ):
        return None, "missing_price"
    if any(quantity is None for quantity, _ in components):
        return None, "missing_usage"
    numerator = sum(quantity * rate for quantity, rate in components if quantity is not None and rate is not None)
    # 每次调用只舍入一次，按最小金额单位四舍五入。
    return (numerator + denominator // 2) // denominator, "priced"


def estimate_amount_nano(
    model_type: str, measurement: UsageMeasurement, *,
    input_price_nano: int | None = None, output_price_nano: int | None = None, image_price_nano: int | None = None,
    cached_input_price_nano: int | None = None,
) -> int | None:
    amount, _ = _price_measurement(
        model_type, measurement, input_price_nano=input_price_nano,
        output_price_nano=output_price_nano, image_price_nano=image_price_nano,
        cached_input_price_nano=cached_input_price_nano,
    )
    return amount


def _insert(db: AsyncSession):
    return pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert


async def get_price(db: AsyncSession, cfg: PriceConfig) -> AiModelPrice | None:
    return await db.scalar(select(AiModelPrice).where(
        AiModelPrice.config_instance_key == cfg.instance_key, AiModelPrice.provider == cfg.provider,
        AiModelPrice.model == cfg.model, AiModelPrice.model_type == cfg.model_type,
    ).execution_options(populate_existing=True))


def _validate_rates(
    model_type: str, input_price_nano: int | None, output_price_nano: int | None, image_price_nano: int | None,
    cached_input_price_nano: int | None,
) -> None:
    rates = input_price_nano, output_price_nano, image_price_nano, cached_input_price_nano
    if any(rate is not None and (type(rate) is not int or not 0 <= rate <= MAX_RATE_NANO) for rate in rates):
        raise ValueError("单价币种或范围无效")
    match model_type:
        case "text":
            valid = image_price_nano is None and (input_price_nano is not None or output_price_nano is not None)
        case "embedding":
            valid = input_price_nano is not None and output_price_nano is None and image_price_nano is None
        case "image":
            valid = image_price_nano is not None and input_price_nano is None and output_price_nano is None
        case _:
            valid = False
    if not valid or (model_type != "text" and cached_input_price_nano is not None):
        raise ValueError("单价须与模型计量单位匹配")


async def set_price(
    db: AsyncSession, cfg: PriceConfig, *, currency: str,
    input_price_nano: int | None = None, output_price_nano: int | None = None, image_price_nano: int | None = None,
    off_peak: OffPeakPrice | None = None,
    cached_input_price_nano: int | None = None,
) -> AiModelPrice:
    if currency not in CURRENCIES:
        raise ValueError("单价币种或范围无效")
    _validate_rates(cfg.model_type, input_price_nano, output_price_nano, image_price_nano, cached_input_price_nano)
    if off_peak is not None:
        _validate_rates(
            cfg.model_type, off_peak.input_price_nano, off_peak.output_price_nano, off_peak.image_price_nano,
            off_peak.cached_input_price_nano,
        )
    values = {
        "currency": currency, "provider": cfg.provider, "model": cfg.model, "model_type": cfg.model_type,
        "input_price_nano": input_price_nano, "output_price_nano": output_price_nano,
        "cached_input_price_nano": cached_input_price_nano,
        "image_price_nano": image_price_nano, "updated_at": func.now(),
        "off_peak": _price_payload(off_peak) if off_peak is not None else None,
    }
    statement = (
        _insert(db)(AiModelPrice).values(config_instance_key=cfg.instance_key, **values)
        .on_conflict_do_update(index_elements=[AiModelPrice.config_instance_key], set_=values)
        .returning(AiModelPrice).execution_options(populate_existing=True)
    )
    return (await db.scalars(statement)).one()


async def delete_price(db: AsyncSession, cfg: PriceConfig) -> None:
    await db.execute(delete(AiModelPrice).where(AiModelPrice.config_instance_key == cfg.instance_key))


def _budget_key(user_id: int | None, project_id: int | None, currency: str) -> dict[str, int | str]:
    if (user_id is None) == (project_id is None) or currency not in CURRENCIES:
        raise ValueError("预算归属或币种无效")
    if user_id is not None:
        return {"user_id": user_id, "currency": currency}
    assert project_id is not None
    return {"project_id": project_id, "currency": currency}


async def get_budget(
    db: AsyncSession, *, user_id: int | None = None, project_id: int | None = None, currency: str,
) -> AiUsageBudget | None:
    return await db.scalar(select(AiUsageBudget).filter_by(**_budget_key(user_id, project_id, currency)))


async def set_budget(
    db: AsyncSession, *, user_id: int | None = None, project_id: int | None = None, currency: str,
    limit_nano: int, threshold_percent: int,
) -> AiUsageBudget:
    key = _budget_key(user_id, project_id, currency)
    if type(limit_nano) is not int or not 0 < limit_nano <= MAX_BUDGET_NANO:
        raise ValueError("预算金额须大于零且不超过十亿")
    if type(threshold_percent) is not int or not 1 <= threshold_percent <= 100:
        raise ValueError("提醒阈值须为 1 到 100 的整数")
    await ai_budget_notifications.lock_budget_scope(db, user_id=user_id, project_id=project_id)
    values = {
        "limit_nano": limit_nano, "threshold_percent": threshold_percent,
        "updated_at": func.now(), "instance_key": str(uuid4()),
    }
    statement = _insert(db)(AiUsageBudget).values(**key, **values).on_conflict_do_update(
        index_elements=list(key), set_=values,
        where=or_(AiUsageBudget.limit_nano != limit_nano, AiUsageBudget.threshold_percent != threshold_percent),
    ).returning(AiUsageBudget).execution_options(populate_existing=True)
    changed = await db.scalar(statement)
    if changed is not None:
        await ai_budget_notifications.reset_progress(db, changed)
        return changed
    return (await db.scalars(select(AiUsageBudget).filter_by(**key).execution_options(populate_existing=True))).one()


async def delete_budget(
    db: AsyncSession, *, user_id: int | None = None, project_id: int | None = None, currency: str,
) -> None:
    key = _budget_key(user_id, project_id, currency)
    await ai_budget_notifications.lock_budget_scope(db, user_id=user_id, project_id=project_id)
    await db.execute(delete(AiUsageBudget).filter_by(**key))


async def price_snapshot(db: AsyncSession, identity: UsageCallIdentity) -> UsagePriceSnapshot | None:
    if identity.config_instance_key is None or identity.payer_id is None:
        return None
    price = await db.scalar(select(AiModelPrice).join(AIConfig).where(
        AIConfig.user_id == identity.payer_id,
        AiModelPrice.config_instance_key == identity.config_instance_key, AiModelPrice.provider == identity.provider,
        AiModelPrice.model == identity.model, AiModelPrice.model_type == identity.model_type,
    ).execution_options(populate_existing=True))
    if price is None:
        return None
    return model_price_snapshot(price)


def _same_created_at(db: AsyncSession, column, expected: datetime):
    timestamp = as_utc(expected)
    if db.get_bind().dialect.name == "sqlite" and timestamp.microsecond == 0:
        # SQLite server_default 和 SQLAlchemy 分别写入秒精度、六位小数的 UTC 文本。
        return or_(column == timestamp, cast(column, String) == timestamp.strftime("%Y-%m-%d %H:%M:%S"))
    return column == timestamp


def _live_user(db: AsyncSession, user_id: int | None, created_at: datetime | None):
    if user_id is None:
        return None
    query = select(User.id).where(User.id == user_id)
    if created_at is not None:
        query = query.where(_same_created_at(db, User.created_at, created_at))
    return query.scalar_subquery()


async def reserve_call(
    db: AsyncSession, identity: UsageCallIdentity, *, call_id: str, started_at: datetime,
    price: UsagePriceSnapshot | None | Literal["current"] = "current", verify_identity: bool = False,
) -> None:
    """在可发送的边界预留尝试。重复持久化保留首次记录和价格。"""
    key = UUID(call_id)
    if key.version != 4 or str(key) != call_id:
        raise ValueError("调用标识无效")
    if price == "current":
        price = await price_snapshot(db, identity)
    schedule = _price_payload(price) if price is not None and price.off_peak is not None else None
    fixed = price if schedule is None else None
    project = None
    if identity.project_id is not None:
        project = select(Project.id).where(Project.id == identity.project_id).scalar_subquery()
    task_conditions = ()
    if identity.task_id is not None and identity.task_key is not None:
        task_conditions = (Task.id == identity.task_id, Task.statistics_key == identity.task_key)
    payer = _live_user(db, identity.payer_id, identity.payer_created_at)
    actor = _live_user(db, identity.actor_id, identity.actor_created_at)
    if verify_identity:
        # 整数账号可被 SQLite 复用。缺原始创建戳的付款方，在同一 INSERT 中使用原配置实例证明。
        if identity.payer_created_at is None:
            payer = None if identity.config_instance_key is None else select(AIConfig.user_id).where(
                AIConfig.instance_key == identity.config_instance_key, AIConfig.user_id == identity.payer_id,
            ).scalar_subquery()
        if identity.actor_created_at is None:
            actor = payer if identity.actor_id == identity.payer_id else None
            if identity.actor_id != identity.payer_id and task_conditions:
                actor = select(Task.user_id).where(
                    *task_conditions, Task.user_id == identity.actor_id,
                ).scalar_subquery()
    author_paid = identity.author_paid
    if project is not None and payer is not None:
        is_owner = select(Project.user_id == payer).where(Project.id == identity.project_id).scalar_subquery()
        author_paid = func.coalesce(is_owner, identity.author_paid)
    chapter = None
    if (project is not None and identity.chapter_id is not None
            and (not verify_identity or identity.chapter_created_at is not None)):
        query = select(Chapter.id).where(Chapter.id == identity.chapter_id, Chapter.project_id == project)
        if identity.chapter_created_at is not None:
            query = query.where(_same_created_at(db, Chapter.created_at, identity.chapter_created_at))
        chapter = query.scalar_subquery()
    task = select(Task.id).where(*task_conditions).scalar_subquery() if task_conditions else None
    await db.execute(
        _insert(db)(AiUsageCall).values(
            id=call_id, started_at=as_utc(started_at), actor_id=actor,
            payer_id=payer, project_id=project, had_project=identity.project_id is not None, chapter_id=chapter,
            task_id=task, task_key=identity.task_key, config_instance_key=identity.config_instance_key,
            author_paid=author_paid, provider=identity.provider, model=identity.model, model_type=identity.model_type,
            kind=identity.kind, currency=price.currency if price else None,
            price_input_nano=fixed.input_price_nano if fixed else None,
            price_cached_input_nano=fixed.cached_input_price_nano if fixed else None,
            price_output_nano=fixed.output_price_nano if fixed else None,
            price_image_nano=fixed.image_price_nano if fixed else None,
            pending_price_schedule=schedule,
        ).on_conflict_do_nothing(index_elements=[AiUsageCall.id])
    )


async def mark_call_sent(db: AsyncSession, call_id: str, *, started_at: datetime | None = None) -> bool:
    values = {"sent": True}
    if started_at is not None:
        values["started_at"] = as_utc(started_at)
    updated = await db.scalar(update(AiUsageCall).where(
        AiUsageCall.id == call_id, AiUsageCall.status == "pending", AiUsageCall.sent.is_(None),
    ).values(**values).returning(AiUsageCall.id))
    return updated is not None


def _call_price(call: AiUsageCall) -> EffectiveUsagePrice | None:
    if call.currency is None:
        return None
    schedule = call.pending_price_schedule
    if schedule is None:
        snapshot = UsagePriceSnapshot(
            currency=call.currency, input_price_nano=call.price_input_nano,
            output_price_nano=call.price_output_nano, image_price_nano=call.price_image_nano,
            cached_input_price_nano=call.price_cached_input_nano,
        )
    else:
        off_peak = OffPeakPrice.from_payload(schedule["off_peak"])
        snapshot = UsagePriceSnapshot(**(schedule | {"off_peak": off_peak}))
    return price_at(snapshot, call.started_at)


async def finish_call(
    db: AsyncSession, call_id: str, *, status: TerminalStatus, sent: bool | None,
    measurement: UsageMeasurement, finished_at: datetime,
) -> bool:
    call = await db.scalar(select(AiUsageCall).where(
        AiUsageCall.id == call_id, AiUsageCall.status == "pending",
    ).with_for_update().execution_options(populate_existing=True))
    if call is None:
        return False
    if call.sent is True:
        sent = True
    if sent is False:
        status = "not_sent"
        measurement = UsageMeasurement()
    measurement = _normalized(call.model_type, measurement)
    amount = None
    price_fields = {}
    if sent is False:
        pricing_status = "not_sent"
    elif sent is None:
        pricing_status = "unconfirmed"
    else:
        selected = _call_price(call)
        if selected is not None:
            price_fields = {
                "price_period": selected.period, "price_input_nano": selected.input_price_nano,
                "price_output_nano": selected.output_price_nano, "price_image_nano": selected.image_price_nano,
                "price_cached_input_nano": selected.cached_input_price_nano,
            }
        amount, pricing_status = _price_measurement(
            call.model_type, measurement, input_price_nano=selected.input_price_nano if selected else None,
            output_price_nano=selected.output_price_nano if selected else None,
            image_price_nano=selected.image_price_nano if selected else None,
            cached_input_price_nano=selected.cached_input_price_nano if selected else None,
        )
    updated = await db.scalar(update(AiUsageCall).where(
        AiUsageCall.id == call_id, AiUsageCall.status == "pending",
    ).values(
        status=status, sent=sent, finished_at=as_utc(finished_at),
        amount_nano=amount, pricing_status=pricing_status, pending_price_schedule=None,
        **price_fields, **asdict(measurement),
    ).returning(AiUsageCall.id))
    if updated is not None and amount:
        await ai_budget_notifications.record_settlement(db, call, amount)
    return updated is not None


async def interrupt_stale_calls(db: AsyncSession, *, before: datetime, finished_at: datetime) -> int:
    result = await db.execute(update(AiUsageCall).where(
        text(PENDING_CALL_WHERE), AiUsageCall.started_at < as_utc(before),
    ).values(
        status="interrupted", finished_at=as_utc(finished_at), pending_price_schedule=None,
        pricing_status=case(
            (AiUsageCall.sent.is_(None), "unconfirmed"),
            (AiUsageCall.currency.is_(None), "missing_price"),
            else_="missing_usage",
        ),
    ).execution_options(synchronize_session=False))
    return result.rowcount
