"""角色 / 组织卡片导入导出（JSON body，不使用 multipart）。"""

from typing import Any

from fastapi import APIRouter

from app.core.deps import CurrentUser, DbDep
from app.schemas.card import CardExportRequest, CardImportRequest, CardImportResult, CardPreview, CardPreviewRequest
from app.services import card_service as svc

router = APIRouter(prefix="/projects/{project_id}/cards", tags=["cards"])


@router.post("/export")
async def export_cards(project_id: int, body: CardExportRequest, user: CurrentUser, db: DbDep) -> dict[str, Any]:
    return await svc.export_cards(db, user.id, project_id, body)


@router.post("/import/preview", response_model=CardPreview)
async def preview_import(project_id: int, body: CardPreviewRequest, user: CurrentUser, db: DbDep) -> CardPreview:
    return await svc.preview_cards(db, user.id, project_id, body.data)


@router.post("/import", response_model=CardImportResult)
async def import_cards(project_id: int, body: CardImportRequest, user: CurrentUser, db: DbDep) -> CardImportResult:
    return await svc.import_cards(db, user.id, project_id, body)
