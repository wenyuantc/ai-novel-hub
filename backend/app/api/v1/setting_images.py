"""书内设定及系列共享设定缩略图。"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, Query, Response, UploadFile
from fastapi.responses import JSONResponse

from app.core.deps import CipherDep, CurrentUser, DbDep, FactoryDep, SettingsDep
from app.schemas.setting_field_value import EntityKind
from app.schemas.setting_image import (
    SeriesThumbnailResult,
    ThumbnailAiApplyRequest,
    ThumbnailAiGenerateOut,
    ThumbnailAiGenerateRequest,
    ThumbnailAiPrompt,
    ThumbnailMeta,
)
from app.services import project_thumbnail_ai, series_thumbnail_ai
from app.services import setting_image_service as svc
from app.services.setting_image_codec import MAX_INPUT_BYTES

router = APIRouter(tags=["setting-thumbnails"])
_OUTPUT_LEASE_SECONDS = 5.0


class _BoundedAiResponse(JSONResponse):
    """发送敏感预览时保留原授权屏障，但慢客户端不能无限阻塞。"""

    async def __call__(self, scope, receive, send) -> None:
        async with asyncio.timeout(_OUTPUT_LEASE_SECONDS):
            await super().__call__(scope, receive, send)


def _ai_response(value: ThumbnailAiPrompt | ThumbnailAiGenerateOut) -> Response:
    return _BoundedAiResponse(content=value.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


def _response(image, requested_etag: str | None) -> Response:
    etag = f'"{image.id}"'
    headers = {"Cache-Control": "private, no-cache", "ETag": etag, "X-Content-Type-Options": "nosniff"}
    if requested_etag == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=image.data, media_type=image.mime_type, headers=headers)


@router.get("/projects/{project_id}/settings/{kind}/{entity_id}/thumbnail")
async def get_book_thumbnail(project_id: int, kind: EntityKind, entity_id: int, user: CurrentUser, db: DbDep,
                             if_none_match: Annotated[str | None, Header()] = None, v: str | None = None) -> Response:
    image = await svc.get_book_image(db, user.id, project_id, kind, entity_id)
    return _response(image, if_none_match)


@router.put("/projects/{project_id}/settings/{kind}/{entity_id}/thumbnail", response_model=ThumbnailMeta)
async def put_book_thumbnail(project_id: int, kind: EntityKind, entity_id: int, user: CurrentUser, db: DbDep,
                             file: Annotated[UploadFile, File()],
                             expected_version: Annotated[str, Form()] = "") -> ThumbnailMeta:
    return await svc.put_book_image(db, user.id, project_id, kind, entity_id,
                                    await file.read(MAX_INPUT_BYTES + 1), file.content_type, expected_version)


@router.delete("/projects/{project_id}/settings/{kind}/{entity_id}/thumbnail")
async def delete_book_thumbnail(project_id: int, kind: EntityKind, entity_id: int, user: CurrentUser, db: DbDep,
                                expected_version: Annotated[str, Query()]) -> None:
    await svc.delete_book_image(db, user.id, project_id, kind, entity_id, expected_version)


@router.get("/series/{sid}/entries/{eid}/thumbnail")
async def get_series_thumbnail(sid: int, eid: int, user: CurrentUser, db: DbDep,
                               if_none_match: Annotated[str | None, Header()] = None, v: str | None = None) -> Response:
    image = await svc.get_series_image(db, user.id, sid, eid)
    return _response(image, if_none_match)


@router.put("/series/{sid}/entries/{eid}/thumbnail", response_model=SeriesThumbnailResult)
async def put_series_thumbnail(sid: int, eid: int, user: CurrentUser, db: DbDep,
                               file: Annotated[UploadFile, File()],
                               expected_revision: Annotated[int, Form(ge=0)],
                               expected_version: Annotated[str, Form()] = "") -> SeriesThumbnailResult:
    return await svc.put_series_image(db, user.id, sid, eid, await file.read(MAX_INPUT_BYTES + 1),
                                      file.content_type, expected_version, expected_revision)


@router.delete("/series/{sid}/entries/{eid}/thumbnail", response_model=SeriesThumbnailResult)
async def delete_series_thumbnail(sid: int, eid: int, user: CurrentUser, db: DbDep,
                                  expected_version: Annotated[str, Query()],
                                  expected_revision: Annotated[int, Query(ge=0)]) -> SeriesThumbnailResult:
    return await svc.delete_series_image(db, user.id, sid, eid, expected_version, expected_revision)


@router.get("/projects/{project_id}/settings/{kind}/{entity_id}/thumbnail/ai/prompt", response_model=ThumbnailAiPrompt)
async def book_thumbnail_prompt(project_id: int, kind: EntityKind, entity_id: int, user: CurrentUser, db: DbDep,
                                cipher: CipherDep) -> Response:
    return _ai_response(await project_thumbnail_ai.default_prompt(db, user.id, project_id, kind, entity_id, cipher))


@router.post("/projects/{project_id}/settings/{kind}/{entity_id}/thumbnail/ai/generate",
             response_model=ThumbnailAiGenerateOut)
async def book_thumbnail_generate(project_id: int, kind: EntityKind, entity_id: int, body: ThumbnailAiGenerateRequest,
                                  user: CurrentUser, db: DbDep, cipher: CipherDep, factory: FactoryDep,
                                  settings: SettingsDep) -> Response:
    result = await project_thumbnail_ai.generate(
        db, user.id, project_id, kind, entity_id, body, cipher, factory,
        ai_timeout=settings.ai_request_timeout, allow_private=settings.allow_private_ai_endpoints,
    )
    return _ai_response(result)


@router.post("/projects/{project_id}/settings/{kind}/{entity_id}/thumbnail/ai/apply", response_model=ThumbnailMeta)
async def book_thumbnail_apply(project_id: int, kind: EntityKind, entity_id: int, body: ThumbnailAiApplyRequest,
                               user: CurrentUser, db: DbDep, cipher: CipherDep, settings: SettingsDep) -> ThumbnailMeta:
    return await project_thumbnail_ai.apply(db, user.id, project_id, kind, entity_id, body, cipher,
                                            allow_private=settings.allow_private_ai_endpoints)


@router.get("/series/{sid}/entries/{eid}/thumbnail/ai/prompt", response_model=ThumbnailAiPrompt)
async def series_thumbnail_prompt(sid: int, eid: int, user: CurrentUser, db: DbDep, cipher: CipherDep) -> Response:
    return _ai_response(await series_thumbnail_ai.default_prompt(db, user.id, sid, eid, cipher))


@router.post("/series/{sid}/entries/{eid}/thumbnail/ai/generate", response_model=ThumbnailAiGenerateOut)
async def series_thumbnail_generate(sid: int, eid: int, body: ThumbnailAiGenerateRequest, user: CurrentUser,
                                    db: DbDep, cipher: CipherDep, factory: FactoryDep,
                                    settings: SettingsDep) -> Response:
    result = await series_thumbnail_ai.generate(db, user.id, sid, eid, body, cipher, factory,
                                                ai_timeout=settings.ai_request_timeout,
                                                allow_private=settings.allow_private_ai_endpoints)
    return _ai_response(result)


@router.post("/series/{sid}/entries/{eid}/thumbnail/ai/apply", response_model=SeriesThumbnailResult)
async def series_thumbnail_apply(sid: int, eid: int, body: ThumbnailAiApplyRequest, user: CurrentUser, db: DbDep,
                                 cipher: CipherDep) -> SeriesThumbnailResult:
    return await series_thumbnail_ai.apply(db, user.id, sid, eid, body, cipher)
