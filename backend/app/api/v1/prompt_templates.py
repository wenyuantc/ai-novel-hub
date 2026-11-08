"""提示词模板：系统默认 + 当前用户的覆盖。固定路径（/export、/import）先于 /{key} 注册。"""

from typing import Any

from fastapi import APIRouter, Response, status

from app.core.deps import CurrentUser, DbDep
from app.schemas.prompt import (
    PromptImportRequest,
    PromptImportResult,
    PromptPatch,
    PromptPreviewOut,
    PromptPreviewRequest,
    PromptSave,
    PromptTemplateBrief,
    PromptTemplateDetail,
    PromptVersionOut,
)
from app.services import prompt_service as svc

router = APIRouter(prefix="/prompt-templates", tags=["prompt-templates"])


@router.get("", response_model=list[PromptTemplateBrief])
async def list_templates(user: CurrentUser, db: DbDep) -> list[PromptTemplateBrief]:
    return await svc.list_templates(db, user.id)


@router.get("/export")
async def export_templates(user: CurrentUser, db: DbDep) -> dict[str, Any]:
    return await svc.export_overrides(db, user.id)


@router.post("/import", response_model=PromptImportResult)
async def import_templates(body: PromptImportRequest, user: CurrentUser, db: DbDep) -> PromptImportResult:
    return await svc.import_overrides(db, user.id, body.data)


@router.get("/{key}", response_model=PromptTemplateDetail)
async def get_template(key: str, user: CurrentUser, db: DbDep) -> PromptTemplateDetail:
    return await svc.get_template(db, user.id, key)


@router.put("/{key}", response_model=PromptTemplateDetail)
async def save_template(key: str, body: PromptSave, user: CurrentUser, db: DbDep) -> PromptTemplateDetail:
    return await svc.save_override(db, user.id, key, body)


@router.patch("/{key}", response_model=PromptTemplateDetail)
async def patch_template(key: str, body: PromptPatch, user: CurrentUser, db: DbDep) -> PromptTemplateDetail:
    return await svc.patch_override(db, user.id, key, body)


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def reset_template(key: str, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_override(db, user.id, key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{key}/versions", response_model=list[PromptVersionOut])
async def list_versions(key: str, user: CurrentUser, db: DbDep):
    return await svc.list_versions(db, user.id, key)


@router.post("/{key}/versions/{version_id}/restore", response_model=PromptTemplateDetail)
async def restore_version(key: str, version_id: int, user: CurrentUser, db: DbDep) -> PromptTemplateDetail:
    return await svc.restore_version(db, user.id, key, version_id)


@router.post("/{key}/preview", response_model=PromptPreviewOut)
async def preview_template(key: str, body: PromptPreviewRequest, user: CurrentUser, db: DbDep) -> PromptPreviewOut:
    return await svc.preview(db, user.id, key, body)
