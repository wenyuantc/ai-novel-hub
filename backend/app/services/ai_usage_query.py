"""用量读权限、账单投影及模型单价和预算管理。"""

import json
from datetime import datetime
from hashlib import sha256

from sqlalchemy import and_, case, func, null, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import AIConfig, Chapter, Project, ProjectCollaborator, User
from app.models.ai_usage import AiModelPrice, AiUsageBudget, AiUsageCall, AiUsageTracking
from app.schemas.ai_usage import (
    CURRENCIES,
    AdminUsageFilters,
    BudgetInput,
    BudgetProgress,
    BudgetState,
    Currency,
    ModelOffPeakPrice,
    ModelOffPeakWindow,
    ModelPrice,
    ModelPriceOut,
    ModelPriceUpdate,
    UsageCurrencyTotal,
    UsageFilters,
    UsagePage,
    UsageRow,
    UsageSummary,
)
from app.services import (
    ai_config_service,
    ai_usage_storage,
    authorization_service,
    personal_projection,
    project_lifecycle,
)


async def _personal_scope(
    db: AsyncSession, actor_id: int, project_id: int | None, chapter_id: int | None,
) -> int | None:
    if project_id is not None:
        await personal_projection.require_personal_project(db, actor_id, project_id)
    if chapter_id is None:
        return project_id

    chapter_project = await db.scalar(select(Chapter.project_id).where(Chapter.id == chapter_id))
    if chapter_project is None or (project_id is not None and chapter_project != project_id):
        raise NotFoundError("章节不存在")
    if project_id is None:
        try:
            await personal_projection.require_personal_project(db, actor_id, chapter_project)
        except NotFoundError:
            raise NotFoundError("章节不存在") from None
    return chapter_project


def _visible_calls(actor_id: int | None):
    call = AiUsageCall
    actor, payer = aliased(User), aliased(User)
    if actor_id is None:
        can_price = can_actor = can_task = can_project = true()
        scope = true()
    else:
        # 个人财务历史在撤销协作后仍保留，作品及任务链接需当前独立访问凭据。
        membership = select(ProjectCollaborator.id).where(
            ProjectCollaborator.project_id == Project.id,
            ProjectCollaborator.user_id == actor_id,
            ProjectCollaborator.status == "accepted",
        ).exists()
        can_project = and_(
            *project_lifecycle.active_conditions(),
            or_(Project.user_id == actor_id, membership),
        )
        can_price = call.payer_id == actor_id
        can_actor = or_(call.actor_id == actor_id, and_(can_price, can_project))
        can_task = and_(call.actor_id == actor_id, or_(can_project, ~call.had_project))
        scope = or_(call.actor_id == actor_id, can_price)

    def allowed(condition, field, name: str):
        return case((condition, field), else_=null()).label(name)

    return select(
        call.id,
        call.started_at,
        allowed(can_actor, call.actor_id, "actor_id"),
        allowed(can_actor, actor.username, "actor_name"),
        allowed(can_price, call.payer_id, "payer_id"),
        allowed(can_price, payer.username, "payer_name"),
        allowed(can_project, call.project_id, "project_id"),
        allowed(can_project, Project.title, "project_title"),
        allowed(can_project, call.chapter_id, "chapter_id"),
        allowed(can_task, call.task_id, "task_id"),
        allowed(can_price, call.provider, "provider"),
        allowed(can_price, call.model, "model"),
        allowed(can_price, call.config_instance_key, "config_instance_key"),
        call.kind,
        call.model_type,
        call.status,
        call.input_tokens,
        call.cached_input_tokens,
        call.output_tokens,
        call.image_count,
        call.input_source,
        call.output_source,
        call.image_source,
        allowed(can_price, call.currency, "currency"),
        allowed(can_price, call.amount_nano, "amount_nano"),
        allowed(can_price, call.price_input_nano, "price_input_nano"),
        allowed(can_price, call.price_cached_input_nano, "price_cached_input_nano"),
        allowed(can_price, call.price_output_nano, "price_output_nano"),
        allowed(can_price, call.price_image_nano, "price_image_nano"),
        allowed(can_price, call.price_period, "price_period"),
        case((can_price, call.pricing_status), else_="private").label("pricing_status"),
        case(
            (call.payer_id.is_(None), "unknown"),
            (call.payer_id == call.actor_id, "own"),
            (call.author_paid.is_(True), "author"),
            else_="other",
        ).label("billing_source"),
        case(
            (and_(call.project_id.is_not(None), can_project), "linked"),
            (call.project_id.is_not(None), "private"),
            (call.had_project.is_(True), "deleted"),
            else_="none",
        ).label("project_state"),
    ).select_from(call).outerjoin(actor, actor.id == call.actor_id).outerjoin(
        payer, payer.id == call.payer_id,
    ).outerjoin(Project, Project.id == call.project_id).where(scope, call.status != "not_sent").cte("visible_usage")


def _sum_cost(amount, prefix: str):
    # SQLite 的 SUM(int64) 可能溢出。先按整币和不足一币分开做整数加总，再在 Python 合并。
    unit = ai_usage_storage.NANOS_PER_UNIT
    return (
        func.coalesce(func.sum(amount.op("/")(unit)), 0).label(f"{prefix}_whole"),
        func.coalesce(func.sum(amount % unit), 0).label(f"{prefix}_remainder"),
    )


def _combined_cost(row, prefix: str) -> int:
    return int(row[f"{prefix}_whole"]) * ai_usage_storage.NANOS_PER_UNIT + int(row[f"{prefix}_remainder"])


def _required_amount(amount_nano: int) -> str:
    result = ai_usage_storage.nano_to_amount(amount_nano)
    assert result is not None
    return result


def _usage_summary(visible):
    c = visible.c
    missing_usage = or_(
        and_(c.model_type == "text", or_(c.input_tokens.is_(None), c.output_tokens.is_(None))),
        and_(c.model_type == "embedding", c.input_tokens.is_(None)),
        and_(c.model_type == "image", c.image_count.is_(None)),
    )
    currency_columns = []
    for currency in CURRENCIES:
        price = case((c.currency == currency, c.amount_nano), else_=null())
        currency_columns.extend(_sum_cost(price, currency))
        currency_columns.append(func.count(price).label(f"{currency}_calls"))
    return select(
        func.count().label("total"),
        func.coalesce(func.sum(c.input_tokens), 0).label("total_input_tokens"),
        func.coalesce(func.sum(c.cached_input_tokens), 0).label("total_cached_input_tokens"),
        func.coalesce(func.sum(c.output_tokens), 0).label("total_output_tokens"),
        func.coalesce(func.sum(c.image_count), 0).label("total_image_count"),
        func.count(case((missing_usage, 1))).label("unknown_usage_calls"),
        func.count(case((and_(c.pricing_status != "private", c.amount_nano.is_(None)), 1))).label("unpriced_calls"),
        *currency_columns,
    ).select_from(visible).cte("usage_summary")


async def list_usage(
    db: AsyncSession, filters: UsageFilters, *, actor_id: int | None,
    project_id: int | None = None, chapter_id: int | None = None,
) -> UsagePage:
    if (project_id is not None and filters.project_id not in (None, project_id)) or (
        chapter_id is not None and filters.chapter_id not in (None, chapter_id)
    ):
        raise BadRequestError("查询范围与路径不一致")
    project_id = project_id or filters.project_id
    chapter_id = chapter_id or filters.chapter_id
    if actor_id is not None:
        project_id = await _personal_scope(db, actor_id, project_id, chapter_id)

    scope = _visible_calls(actor_id)
    fields = {
        "project_id": project_id,
        "chapter_id": chapter_id,
        "provider": filters.provider,
        "model": filters.model,
        "kind": filters.kind,
        "model_type": filters.model_type,
        "status": filters.status,
    }
    if actor_id is None and isinstance(filters, AdminUsageFilters):
        fields.update(actor_id=filters.actor_id, payer_id=filters.payer_id)
    criteria = [scope.c[key] == value for key, value in fields.items() if value is not None]
    if filters.config_id is not None:
        config = select(AIConfig.instance_key).where(AIConfig.id == filters.config_id)
        if actor_id is not None:
            config = config.where(AIConfig.user_id == actor_id)
        criteria.append(scope.c.config_instance_key == config.scalar_subquery())
    if filters.from_at is not None:
        criteria.append(scope.c.started_at >= filters.from_at)
    if filters.to_at is not None:
        criteria.append(scope.c.started_at < filters.to_at)

    filtered = select(scope).where(*criteria).cte("filtered_usage")
    summary = _usage_summary(filtered)
    page = select(filtered).order_by(filtered.c.started_at.desc(), filtered.c.id.desc()).limit(
        filters.page_size,
    ).offset((filters.page - 1) * filters.page_size).subquery()
    tracking_since = select(AiUsageTracking.started_at).where(AiUsageTracking.id == 1).scalar_subquery()
    # 单条语句同时读取明细与摘要，避免并发调用写入导致计数与页不一致。
    stmt = select(page, summary, tracking_since.label("tracking_since")).select_from(
        summary.outerjoin(page, true()),
    ).order_by(page.c.started_at.desc(), page.c.id.desc())
    result = (await db.execute(stmt)).mappings().all()
    first = result[0]
    items = []
    for row in result:
        if row["id"] is None:
            continue
        items.append(UsageRow.model_validate({
            **row,
            "started_at": project_lifecycle.as_utc(row["started_at"]),
            "estimated_cost": ai_usage_storage.nano_to_amount(row["amount_nano"]),
            "input_per_million": ai_usage_storage.nano_to_amount(row["price_input_nano"]),
            "cached_input_per_million": ai_usage_storage.nano_to_amount(row["price_cached_input_nano"]),
            "output_per_million": ai_usage_storage.nano_to_amount(row["price_output_nano"]),
            "per_image": ai_usage_storage.nano_to_amount(row["price_image_nano"]),
        }))
    since = first["tracking_since"]
    return UsagePage(
        items=items,
        total=first["total"],
        page=filters.page,
        page_size=filters.page_size,
        tracking_since=project_lifecycle.as_utc(since) if since is not None else None,
        summary=UsageSummary(
            input_tokens=first["total_input_tokens"],
            cached_input_tokens=first["total_cached_input_tokens"],
            output_tokens=first["total_output_tokens"],
            image_count=first["total_image_count"],
            unknown_usage_calls=first["unknown_usage_calls"],
            unpriced_calls=first["unpriced_calls"],
            costs=[UsageCurrencyTotal(
                currency=currency,
                amount=_required_amount(_combined_cost(first, currency)),
                calls=first[f"{currency}_calls"],
            ) for currency in CURRENCIES if first[f"{currency}_calls"]],
        ),
    )


def _price_view(config: AIConfig, row: AiModelPrice | None) -> ModelPriceOut:
    price = None
    version_price = None
    if row is not None:
        snapshot = ai_usage_storage.model_price_snapshot(row)
        off_peak = snapshot.off_peak
        windows = [] if off_peak is None else [
            ModelOffPeakWindow(start=window.start, end=window.end) for window in off_peak.additional_windows
        ]
        price = ModelPrice(
            currency=row.currency,
            input_per_million=ai_usage_storage.nano_to_amount(row.input_price_nano),
            cached_input_per_million=ai_usage_storage.nano_to_amount(snapshot.cached_input_price_nano),
            output_per_million=ai_usage_storage.nano_to_amount(row.output_price_nano),
            per_image=ai_usage_storage.nano_to_amount(row.image_price_nano),
            off_peak=None if off_peak is None else ModelOffPeakPrice(
                timezone=off_peak.timezone,
                start=off_peak.start,
                end=off_peak.end,
                input_per_million=ai_usage_storage.nano_to_amount(off_peak.input_price_nano),
                cached_input_per_million=ai_usage_storage.nano_to_amount(off_peak.cached_input_price_nano),
                output_per_million=ai_usage_storage.nano_to_amount(off_peak.output_price_nano),
                per_image=ai_usage_storage.nano_to_amount(off_peak.image_price_nano),
                **({"additional_windows": windows} if windows else {}),
            ),
        )
        # 仅省略后来新增的未配置字段，保持旧固定价和分时价的版本指纹。
        version_price = price.model_dump()
        if snapshot.cached_input_price_nano is None:
            del version_price["cached_input_per_million"]
        if off_peak is None:
            del version_price["off_peak"]
        elif off_peak.cached_input_price_nano is None:
            del version_price["off_peak"]["cached_input_per_million"]
    identity = json.dumps([
        config.instance_key, config.provider, config.model, config.model_type, version_price,
    ], ensure_ascii=False)
    return ModelPriceOut(
        version=sha256(identity.encode()).hexdigest(),
        provider=config.provider,
        model=config.model,
        model_type=config.model_type,
        price=price,
    )


async def get_model_price(db: AsyncSession, actor_id: int, config_id: int) -> ModelPriceOut:
    config = await ai_config_service.get_config(db, actor_id, config_id)
    return _price_view(config, await ai_usage_storage.get_price(db, config))


def _rates_in_nanos(
    price: ModelPrice | ModelOffPeakPrice, *, existing_cached_rate: int | None, currency_changed: bool,
) -> dict[str, int | None]:
    rates = {
        key: ai_usage_storage.amount_to_nano(value) if value is not None else None
        for key, value in (
            ("input_price_nano", price.input_per_million),
            ("cached_input_price_nano", price.cached_input_per_million),
            ("output_price_nano", price.output_per_million),
            ("image_price_nano", price.per_image),
        )
    }
    if existing_cached_rate is not None and "cached_input_per_million" not in price.model_fields_set:
        if currency_changed:
            raise ValueError("变更币种时请同时提交缓存命中单价或将其清空")
        rates["cached_input_price_nano"] = existing_cached_rate
    return rates


async def update_model_price(
    db: AsyncSession, actor_id: int, config_id: int, body: ModelPriceUpdate,
) -> ModelPriceOut:
    await authorization_service.require_authenticated(db, actor_id, write=True)
    await ai_config_service.lock_config_owner(db, actor_id, write=True)
    config = await ai_config_service.get_config(db, actor_id, config_id)
    existing = await ai_usage_storage.get_price(db, config)
    if _price_view(config, existing).version != body.expected_version:
        raise ConflictError("模型配置或单价已变化，请刷新后重试", extra={"code": "model_price_changed"})
    row = None
    try:
        if body.price is None:
            await ai_usage_storage.delete_price(db, config)
        else:
            price = body.price
            existing_snapshot = ai_usage_storage.model_price_snapshot(existing) if existing is not None else None
            existing_off_peak = existing_snapshot.off_peak if existing_snapshot is not None else None
            currency_changed = existing_snapshot is not None and price.currency != existing_snapshot.currency
            off_peak = None
            proposed_off_peak = price.off_peak
            if proposed_off_peak is not None:
                windows = (
                    existing_off_peak.additional_windows
                    if existing_off_peak is not None and "additional_windows" not in proposed_off_peak.model_fields_set
                    else tuple(
                        ai_usage_storage.OffPeakWindow(start=window.start, end=window.end)
                        for window in proposed_off_peak.additional_windows
                    )
                )
                off_peak = ai_usage_storage.OffPeakPrice(
                    timezone=proposed_off_peak.timezone, start=proposed_off_peak.start, end=proposed_off_peak.end,
                    additional_windows=windows,
                    **_rates_in_nanos(
                        proposed_off_peak,
                        existing_cached_rate=existing_off_peak.cached_input_price_nano if existing_off_peak else None,
                        currency_changed=currency_changed,
                    ),
                )
            elif existing_off_peak is not None and "off_peak" not in price.model_fields_set:
                if currency_changed:
                    raise ValueError("变更币种时请同时提交空闲单价或关闭分时定价")
                off_peak = existing_off_peak
            row = await ai_usage_storage.set_price(
                db, config, currency=price.currency, off_peak=off_peak,
                **_rates_in_nanos(
                    price,
                    existing_cached_rate=existing_snapshot.cached_input_price_nano if existing_snapshot else None,
                    currency_changed=currency_changed,
                ),
            )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from None
    response = _price_view(config, row)
    await db.commit()
    return response


async def _require_budget_project(db: AsyncSession, actor_id: int, project_id: int, *, write: bool = False) -> None:
    if write:
        await authorization_service.require_authenticated(db, actor_id, write=True)
    project = await project_lifecycle.load_record(db, project_id)
    if project is None or project.user_id != actor_id:
        raise NotFoundError("项目不存在")
    if write:
        project = await project_lifecycle.lock_active(db, project_id)
        if project.user_id != actor_id:
            raise NotFoundError("项目不存在")
    project_lifecycle.ensure_active(project)


async def budget_progress(
    db: AsyncSession, actor_id: int, *, project_id: int | None = None, now: datetime | None = None,
) -> list[BudgetProgress]:
    budget, call = AiUsageBudget, AiUsageCall
    period_start, period_end = None, None
    if project_id is None:
        scope = "account"
        budget_scope = budget.user_id == actor_id
        period_start, period_end = ai_usage_storage.monthly_budget_window(now or project_lifecycle.now_utc())
        call_scope = and_(call.started_at >= period_start, call.started_at < period_end)
    else:
        await _require_budget_project(db, actor_id, project_id)
        scope = "project"
        budget_scope = budget.project_id == project_id
        call_scope = and_(call.project_id == project_id, call.author_paid.is_(True))

    join_on = and_(
        call.payer_id == actor_id,
        call.status != "not_sent",
        call_scope,
        or_(call.currency == budget.currency, call.currency.is_(None)),
    )
    stmt = select(
        budget.currency, budget.limit_nano, budget.threshold_percent,
        *_sum_cost(call.amount_nano, "spent"),
        func.count(case((and_(call.id.is_not(None), call.amount_nano.is_(None)), 1))).label("unpriced_calls"),
    ).select_from(budget).outerjoin(call, join_on).where(budget_scope).group_by(
        budget.currency, budget.limit_nano, budget.threshold_percent,
    ).order_by(budget.currency)
    progress = []
    for row in (await db.execute(stmt)).mappings():
        spent = _combined_cost(row, "spent")
        limit = row["limit_nano"]
        threshold = row["threshold_percent"]
        state: BudgetState = "normal"
        if spent >= limit:
            state = "exceeded"
        elif spent * 100 >= limit * threshold:
            state = "near"
        progress.append(BudgetProgress(
            scope=scope,
            currency=row["currency"],
            limit=_required_amount(limit),
            threshold_percent=threshold,
            spent=_required_amount(spent),
            unpriced_calls=row["unpriced_calls"],
            state=state,
            period_start=period_start,
            period_end=period_end,
        ))
    return progress


async def update_budget(
    db: AsyncSession, actor_id: int, currency: Currency, body: BudgetInput | None, *, project_id: int | None = None,
) -> BudgetProgress | None:
    if project_id is None:
        await authorization_service.require_authenticated(db, actor_id, write=True)
    else:
        await _require_budget_project(db, actor_id, project_id, write=True)
    user_id = actor_id if project_id is None else None
    response = None
    try:
        if body is None:
            await ai_usage_storage.delete_budget(db, user_id=user_id, project_id=project_id, currency=currency)
        else:
            await ai_usage_storage.set_budget(
                db, user_id=user_id, project_id=project_id, currency=currency,
                limit_nano=ai_usage_storage.amount_to_nano(body.limit), threshold_percent=body.threshold_percent,
            )
            await db.flush()
            results = await budget_progress(db, actor_id, project_id=project_id)
            response = next(item for item in results if item.currency == currency)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from None
    await db.commit()
    return response
