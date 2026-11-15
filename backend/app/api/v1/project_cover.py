import asyncio
from typing import Annotated

from fastapi import APIRouter, File, Response, UploadFile
from fastapi.responses import JSONResponse

from app.ai.images import MAX_COVER_BYTES
from app.core.deps import CipherDep, CurrentUser, DbDep, FactoryDep, SettingsDep
from app.schemas.project import ProjectOut
from app.schemas.project_cover import CoverGenerateOut, CoverGenerateRequest, CoverPromptOut
from app.services import cover_service as svc

router = APIRouter(prefix="/projects/{project_id}/cover", tags=["project-cover"])
_OUTPUT_LEASE_SECONDS = 5.0


class _BoundedCoverResponse(JSONResponse):
    """授权锁与响应同寿命，但慢客户端不能无限阻塞撤权。"""

    async def __call__(self, scope, receive, send) -> None:
        async with asyncio.timeout(_OUTPUT_LEASE_SECONDS):
            await super().__call__(scope, receive, send)


@router.get("")
async def get_cover(project_id: int, user: CurrentUser, db: DbDep, v: str | None = None) -> Response:
    cover = await svc.get_manual_cover(db, user.id, project_id)
    return Response(
        content=cover.data,
        media_type=cover.mime_type,
        headers={"Cache-Control": "private, max-age=31536000, immutable" if v else "private, no-cache"},
    )


@router.put("", response_model=ProjectOut)
async def put_cover(
    project_id: int,
    user: CurrentUser,
    db: DbDep,
    file: Annotated[UploadFile, File(description="JPG / PNG / WebP，≤ 10MB")],
) -> ProjectOut:
    data = await file.read(MAX_COVER_BYTES + 1)
    return await svc.put_cover(db, user.id, project_id, data)


@router.delete("", response_model=ProjectOut)
async def delete_cover(project_id: int, user: CurrentUser, db: DbDep) -> ProjectOut:
    return await svc.delete_cover(db, user.id, project_id)


@router.get("/prompt", response_model=CoverPromptOut)
async def default_prompt(project_id: int, user: CurrentUser, db: DbDep) -> CoverPromptOut | Response:
    result = await svc.default_prompt(db, user.id, project_id)
    return _BoundedCoverResponse(content=result.model_dump(mode="json"))


@router.post("/generate", response_model=CoverGenerateOut, response_model_exclude_none=True)
async def generate_cover(
    project_id: int,
    body: CoverGenerateRequest,
    user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    cipher: CipherDep,
    factory: FactoryDep,
) -> CoverGenerateOut | Response:
    result = await svc.generate_cover(
        db, user.id, project_id, body, cipher, factory,
        ai_timeout=settings.ai_request_timeout, allow_private=settings.allow_private_ai_endpoints,
    )
    if result.model_source is not None:
        return _BoundedCoverResponse(content=result.model_dump(mode="json", exclude_none=True))
    return result
