"""写作风格库。固定路径（/export、/import、/extract、/trial）先于 /{style_id} 注册。"""

from typing import Any

from fastapi import APIRouter, Query, Response, status
from fastapi.responses import StreamingResponse

from app.api.sse import sse_response
from app.core.deps import CipherDep, CurrentUser, DbDep, FactoryDep, SettingsDep
from app.schemas.style import (
    StyleCreate,
    StyleExtractRequest,
    StyleImportRequest,
    StyleImportResult,
    StyleOut,
    StyleTrialRequest,
    StyleUpdate,
)
from app.services import style_service as svc

router = APIRouter(prefix="/styles", tags=["styles"])


@router.get("", response_model=list[StyleOut])
async def list_styles(user: CurrentUser, db: DbDep) -> list[StyleOut]:
    return await svc.list_styles(db, user.id)


@router.post("", response_model=StyleOut, status_code=status.HTTP_201_CREATED)
async def create_style(body: StyleCreate, user: CurrentUser, db: DbDep) -> StyleOut:
    return await svc.create_style(db, user.id, body)


@router.get("/export")
async def export_styles(user: CurrentUser, db: DbDep, ids: list[int] | None = Query(default=None)) -> dict[str, Any]:
    return await svc.export_styles(db, user.id, ids)


@router.post("/import", response_model=StyleImportResult)
async def import_styles(body: StyleImportRequest, user: CurrentUser, db: DbDep) -> StyleImportResult:
    return await svc.import_styles(db, user.id, body.data)


@router.post("/extract", response_class=StreamingResponse)
async def extract_style(
    body: StyleExtractRequest,
    user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    cipher: CipherDep,
    factory: FactoryDep,
) -> StreamingResponse:
    job = await svc.prepare_extract(
        db,
        user_id=user.id,
        req=body,
        cipher=cipher,
        factory=factory,
        ai_timeout=settings.ai_request_timeout,
        allow_private=settings.allow_private_ai_endpoints,
    )
    await db.close()
    return sse_response(svc.run_extract(job))


@router.post("/trial", response_class=StreamingResponse)
async def trial_style(
    body: StyleTrialRequest,
    user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    cipher: CipherDep,
    factory: FactoryDep,
) -> StreamingResponse:
    job = await svc.prepare_trial(
        db,
        user_id=user.id,
        req=body,
        cipher=cipher,
        factory=factory,
        ai_timeout=settings.ai_request_timeout,
        allow_private=settings.allow_private_ai_endpoints,
    )
    await db.close()
    return sse_response(svc.run_trial(job))


@router.get("/{style_id}", response_model=StyleOut)
async def get_style(style_id: int, user: CurrentUser, db: DbDep) -> StyleOut:
    return await svc.get_style(db, user.id, style_id)


@router.patch("/{style_id}", response_model=StyleOut)
async def update_style(style_id: int, body: StyleUpdate, user: CurrentUser, db: DbDep) -> StyleOut:
    return await svc.update_style(db, user.id, style_id, body)


@router.delete("/{style_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_style(style_id: int, user: CurrentUser, db: DbDep, force: bool = False) -> Response:
    await svc.delete_style(db, user.id, style_id, force=force)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{style_id}/copy", response_model=StyleOut, status_code=status.HTTP_201_CREATED)
async def copy_style(style_id: int, user: CurrentUser, db: DbDep) -> StyleOut:
    return await svc.copy_style(db, user.id, style_id)
