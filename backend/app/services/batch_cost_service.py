"""批量生成的只读预估。实际结算口径以用量记录中的价格和数量为准。"""

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, NotFoundError
from app.core.security import SecretCipher
from app.models import Chapter, Project, Scene
from app.schemas.batch import (
    BatchBudgetEstimate,
    BatchCostEstimate,
    BatchCostStep,
    BatchGenerateRequest,
    BatchPreviewItem,
)
from app.services import (
    ai_config_service,
    ai_usage_storage,
    project_ai_config,
    project_lifecycle,
    scene_service,
    skill_service,
    style_service,
)
from app.services.ai_usage_query import budget_progress

# 每次调用的固定模板和格式指令预留。分析输出为结构化摘要，随目标篇幅估计。
GENERATION_TEMPLATE_TOKENS = 1500
ANALYSIS_TEMPLATE_TOKENS = 2000
MIN_ANALYSIS_OUTPUT_TOKENS = 800


def _estimated_text_tokens(chars: int) -> int:
    return int(chars / ai_config_service.CHARS_PER_TOKEN)


async def estimate_batch_cost(
    db: AsyncSession, settings: Any, cipher: SecretCipher, actor_id: int, project: Project,
    request: BatchGenerateRequest, chapters: list[Chapter], items: list[BatchPreviewItem], analyze: bool,
) -> BatchCostEstimate | None:
    if not items:
        return None
    scene_rows = await db.execute(
        select(Scene.chapter_id, func.count()).where(
            Scene.chapter_id.in_([item.chapter_id for item in items]), Scene.active.is_(True),
        ).group_by(Scene.chapter_id),
    )
    scene_counts = {chapter_id: count for chapter_id, count in scene_rows}
    targets = {
        item.chapter_id: scene_service.split_scene_target_words(item.target_words, scene_counts.get(item.chapter_id, 0))
        for item in items
    }
    steps = [BatchCostStep(kind="generation", calls=sum(scene_counts.values()))]
    if analyze:
        steps.append(BatchCostStep(kind="analysis", calls=len(items)))
    try:
        binding, cfg = await project_ai_config.preview_project_text_model(
            db, actor_id, project.id, cipher=cipher, config_id=request.ai_config_id,
            allow_private=settings.allow_private_ai_endpoints,
        )
    except (BadRequestError, NotFoundError):
        return BatchCostEstimate(status="unavailable", steps=steps)
    if binding.source == "owner":
        return BatchCostEstimate(status="author_paid", steps=steps)

    context_budget = ai_config_service.context_char_budget(cfg, settings.context_char_budget)
    output_limit = ai_config_service.max_output_tokens(cfg)
    skill = await skill_service.snapshot(db, actor_id, request.skill_id, request.skill_inputs)
    added_chars = len(request.instruction) + len(skill["body"] if skill else "")
    generation_input = generation_output = analysis_input = analysis_output = 0
    by_id = {chapter.id: chapter for chapter in chapters}
    styles: dict[int | str | None, int] = {}
    for item in items:
        chapter = by_id[item.chapter_id]
        style_key = chapter.style_id if request.style == "inherit" else request.style
        if style_key not in styles:
            resolved = await style_service.resolve(db, actor_id, project, chapter, request.style)
            styles[style_key] = len(resolved.block) if resolved else 0
        scene_outputs = [_estimated_text_tokens(words) for words in targets[chapter.id]]
        expected_output = sum(scene_outputs)
        if output_limit is not None:
            expected_output = sum(min(tokens, output_limit) for tokens in scene_outputs)
        generation_input += len(scene_outputs) * (GENERATION_TEMPLATE_TOKENS + _estimated_text_tokens(
            context_budget + added_chars + styles[style_key],
        ))
        generation_output += expected_output
        if analyze:
            analysis_input += ANALYSIS_TEMPLATE_TOKENS + expected_output + _estimated_text_tokens(
                min(3000, context_budget // 4),
            )
            expected_analysis_output = max(MIN_ANALYSIS_OUTPUT_TOKENS, expected_output // 5)
            analysis_output += (expected_analysis_output if output_limit is None
                                else min(output_limit, expected_analysis_output))
    steps[0].estimated_input_tokens, steps[0].estimated_output_tokens = generation_input, generation_output
    if analyze:
        steps[1].estimated_input_tokens, steps[1].estimated_output_tokens = analysis_input, analysis_output

    model_price = await ai_usage_storage.get_price(db, cfg)
    if model_price is None:
        return BatchCostEstimate(status="missing_price", steps=steps)
    now = project_lifecycle.now_utc()
    price = ai_usage_storage.price_at(ai_usage_storage.model_price_snapshot(model_price), now)
    total = 0
    for step in steps:
        amount = ai_usage_storage.estimate_amount_nano(
            "text", ai_usage_storage.UsageMeasurement(
                input_tokens=step.estimated_input_tokens, output_tokens=step.estimated_output_tokens,
                input_source="estimated", output_source="estimated",
            ),
            input_price_nano=price.input_price_nano, output_price_nano=price.output_price_nano, image_price_nano=None,
        )
        if amount is None:
            return BatchCostEstimate(
                status="missing_price", steps=steps, currency=price.currency,
                price_period=price.period, price_timezone=price.timezone,
            )
        step.estimated_amount = ai_usage_storage.nano_to_amount(amount)
        total += amount
    estimated_amount = ai_usage_storage.nano_to_amount(total)

    budgets = await budget_progress(db, actor_id, now=now)
    if actor_id == binding.project_owner_id:
        budgets += await budget_progress(db, actor_id, project_id=project.id, now=now)
    projected = []
    for budget in budgets:
        if budget.currency == price.currency:
            spent = Decimal(budget.spent) + Decimal(estimated_amount)
            limit = Decimal(budget.limit)
            state = "normal"
            if spent >= limit:
                state = "exceeded"
            elif spent * 100 >= limit * budget.threshold_percent:
                state = "near"
            projected.append(BatchBudgetEstimate(
                scope=budget.scope, limit=budget.limit, projected_spent=format(spent, "f"), state=state,
                unpriced_calls=budget.unpriced_calls,
            ))
    return BatchCostEstimate(
        status="estimated", currency=price.currency, estimated_amount=estimated_amount, steps=steps, budgets=projected,
        price_period=price.period, price_timezone=price.timezone,
        assumes_uncached_input=price.cached_input_price_nano is not None,
    )
