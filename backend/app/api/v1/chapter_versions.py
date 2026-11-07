"""章节版本、生成记录与局部改写路由（从 chapters.py 拆出，避免单文件过长）。"""

from fastapi import APIRouter, Query, Response, status
from fastapi.responses import StreamingResponse

from app.api.sse import sse_response
from app.core.deps import CipherDep, CurrentUser, DbDep, FactoryDep, SessionmakerDep, SettingsDep
from app.models import Chapter, ChapterVersion, GenerationLog
from app.schemas.chapter import ChapterOut
from app.schemas.chapter_version import (
    ChapterVersionBrief,
    ChapterVersionOut,
    ChapterVersionUpdate,
    GenerationLogBrief,
    GenerationLogOut,
    RewriteApply,
    RewriteRequest,
    VersionExtract,
    VersionRestore,
)
from app.schemas.scene import SceneOut
from app.services import chapter_version_service as svc
from app.services import generation_log_service, rewrite_service

router = APIRouter(tags=["chapter-versions"])

_BASE = "/projects/{project_id}/chapters/{chapter_id}"


@router.get(f"{_BASE}/versions", response_model=list[ChapterVersionBrief])
async def list_versions(project_id: int, chapter_id: int, user: CurrentUser, db: DbDep) -> list[ChapterVersion]:
    return await svc.list_versions(db, user.id, project_id, chapter_id)


@router.get(f"{_BASE}/versions/{{version_id}}", response_model=ChapterVersionOut)
async def get_version(
    project_id: int, chapter_id: int, version_id: int, user: CurrentUser, db: DbDep
) -> ChapterVersion:
    return await svc.get_version(db, user.id, project_id, chapter_id, version_id)


@router.patch(f"{_BASE}/versions/{{version_id}}", response_model=ChapterVersionBrief)
async def update_version(
    project_id: int, chapter_id: int, version_id: int, body: ChapterVersionUpdate, user: CurrentUser, db: DbDep
) -> ChapterVersion:
    return await svc.update_version(db, user.id, project_id, chapter_id, version_id, body)


@router.delete(f"{_BASE}/versions/{{version_id}}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_version(project_id: int, chapter_id: int, version_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_version(db, user.id, project_id, chapter_id, version_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(f"{_BASE}/versions/{{version_id}}/restore", response_model=ChapterOut)
async def restore_version(
    project_id: int, chapter_id: int, version_id: int, user: CurrentUser, db: DbDep,
    body: VersionRestore | None = None,
) -> Chapter:
    return await svc.restore_version(db, user.id, project_id, chapter_id, version_id, body)


@router.get(f"{_BASE}/generation-logs", response_model=list[GenerationLogBrief])
async def list_logs(project_id: int, chapter_id: int, user: CurrentUser, db: DbDep) -> list[GenerationLog]:
    return await generation_log_service.list_logs(db, user.id, project_id, chapter_id)


@router.get(f"{_BASE}/generation-logs/{{log_id}}", response_model=GenerationLogOut)
async def get_log(project_id: int, chapter_id: int, log_id: int, user: CurrentUser, db: DbDep) -> GenerationLog:
    return await generation_log_service.get_log(db, user.id, project_id, chapter_id, log_id)


@router.post(f"{_BASE}/rewrite/apply", response_model=ChapterOut)
async def apply_rewrite(project_id: int, chapter_id: int, body: RewriteApply, user: CurrentUser, db: DbDep) -> Chapter:
    return await rewrite_service.apply_rewrite(db, user.id, project_id, chapter_id, body)


@router.post("/chapters/{chapter_id}/rewrite", response_class=StreamingResponse)
async def rewrite_preview(
    chapter_id: int,
    body: RewriteRequest,
    user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    cipher: CipherDep,
    factory: FactoryDep,
    sessionmaker: SessionmakerDep,
) -> StreamingResponse:
    job = await rewrite_service.prepare_rewrite_job(
        db,
        user_id=user.id,
        chapter_id=chapter_id,
        req=body,
        cipher=cipher,
        factory=factory,
        ai_timeout=settings.ai_request_timeout,
        allow_private=settings.allow_private_ai_endpoints,
        context_budget=settings.context_char_budget,
    )
    await db.close()
    return sse_response(rewrite_service.run_rewrite_job(job, sessionmaker))


@router.get(f"{_BASE}/versions/{{version_id}}/restore-preview")
async def restore_preview(
    project_id: int, chapter_id: int, version_id: int, user: CurrentUser, db: DbDep,
    expected_content_revision: int = Query(ge=0),
) -> dict:
    return await svc.restore_preview(db, user.id, project_id, chapter_id, version_id, expected_content_revision)


@router.get(f"{_BASE}/versions/{{version_id}}/extract-preview")
async def extract_preview(
    project_id: int, chapter_id: int, version_id: int, user: CurrentUser, db: DbDep,
    scene_id: int = Query(gt=0), expected_content_revision: int = Query(ge=0),
) -> dict:
    return await svc.extract_preview(db, user.id, project_id, chapter_id, version_id, scene_id,
                                     expected_content_revision)


@router.post(f"{_BASE}/versions/{{version_id}}/extract", response_model=SceneOut)
async def extract_version(
    project_id: int, chapter_id: int, version_id: int, body: VersionExtract, user: CurrentUser, db: DbDep,
) -> SceneOut:
    return await svc.extract_version(db, user.id, project_id, chapter_id, version_id, body)
