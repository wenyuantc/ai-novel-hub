"""个人 AI 用量与预算，以及全站用量读接口。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request, Response

from app.core.deps import CurrentUser, DbDep, EditorUser, require_permission
from app.core.errors import BadRequestError
from app.models import User
from app.schemas.ai_usage import (
    AdminUsageFilters,
    BudgetInput,
    BudgetProgress,
    Currency,
    EntityId,
    ModelPriceOut,
    ModelPriceUpdate,
    UsageFilters,
    UsagePage,
)
from app.services import ai_usage_query as svc

PermissionUsageRead = Annotated[User, Depends(require_permission("ai_usage.read"))]
PersonalFilters = Annotated[UsageFilters, Query()]
BudgetBody = Annotated[BudgetInput | None, Body()]


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


async def _validate_budget_body(request: Request, body: BudgetInput | None) -> None:
    if body is None and (await request.body()).strip() != b"null":
        raise BadRequestError("请提供预算配置，或用 null 清除预算")


router = APIRouter(tags=["ai-usage"], dependencies=[Depends(_no_store)])


@router.get("/ai-usage", response_model=UsagePage)
async def list_usage(user: CurrentUser, db: DbDep, filters: PersonalFilters) -> UsagePage:
    return await svc.list_usage(db, filters, actor_id=user.id)


@router.get("/projects/{project_id}/ai-usage", response_model=UsagePage)
async def list_project_usage(project_id: EntityId, user: CurrentUser, db: DbDep, filters: PersonalFilters) -> UsagePage:
    return await svc.list_usage(db, filters, actor_id=user.id, project_id=project_id)


@router.get("/projects/{project_id}/chapters/{chapter_id}/ai-usage", response_model=UsagePage)
async def list_chapter_usage(
    project_id: EntityId, chapter_id: EntityId, user: CurrentUser, db: DbDep, filters: PersonalFilters,
) -> UsagePage:
    return await svc.list_usage(db, filters, actor_id=user.id, project_id=project_id, chapter_id=chapter_id)


@router.get("/admin/ai-usage", response_model=UsagePage)
async def list_admin_usage(
    user: PermissionUsageRead, db: DbDep, filters: Annotated[AdminUsageFilters, Query()],
) -> UsagePage:
    return await svc.list_usage(db, filters, actor_id=None)


@router.get("/ai-configs/{config_id}/price", response_model=ModelPriceOut)
async def get_model_price(config_id: EntityId, user: CurrentUser, db: DbDep) -> ModelPriceOut:
    return await svc.get_model_price(db, user.id, config_id)


@router.put("/ai-configs/{config_id}/price", response_model=ModelPriceOut)
async def update_model_price(config_id: EntityId, user: EditorUser, db: DbDep, body: ModelPriceUpdate) -> ModelPriceOut:
    return await svc.update_model_price(db, user.id, config_id, body)


@router.get("/ai-usage/budgets", response_model=list[BudgetProgress])
async def list_account_budgets(user: CurrentUser, db: DbDep, budget_key: UUID | None = None) -> list[BudgetProgress]:
    return await svc.budget_progress(db, user.id, budget_key=budget_key)


@router.put("/ai-usage/budgets/{currency}", response_model=BudgetProgress | None)
async def update_account_budget(
    currency: Currency, request: Request, user: EditorUser, db: DbDep, body: BudgetBody = None,
    budget_key: UUID | None = None,
) -> BudgetProgress | None:
    await _validate_budget_body(request, body)
    return await svc.update_budget(db, user.id, currency, body, budget_key=budget_key)


@router.get("/projects/{project_id}/ai-usage/budgets", response_model=list[BudgetProgress])
async def list_project_budgets(
    project_id: EntityId, user: CurrentUser, db: DbDep, budget_key: UUID | None = None,
) -> list[BudgetProgress]:
    return await svc.budget_progress(db, user.id, project_id=project_id, budget_key=budget_key)


@router.put("/projects/{project_id}/ai-usage/budgets/{currency}", response_model=BudgetProgress | None)
async def update_project_budget(
    project_id: EntityId, currency: Currency, request: Request, user: EditorUser, db: DbDep, body: BudgetBody = None,
    budget_key: UUID | None = None,
) -> BudgetProgress | None:
    await _validate_budget_body(request, body)
    return await svc.update_budget(db, user.id, currency, body, project_id=project_id, budget_key=budget_key)
