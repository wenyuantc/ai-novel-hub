from fastapi import APIRouter, Response, status

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.schemas.chapter import (
    ChapterBrief,
    ChapterCreate,
    ChapterOut,
    ChaptersFromOutlines,
    ChapterUpdate,
    GenerateChapterRequest,
    PolishChapterRequest,
)
from app.schemas.progression import SettingContextOut, StoryPosition
from app.schemas.story import ChapterCastOut, ChapterCastUpdate
from app.schemas.task import TaskOut
from app.services import (
    cast_service,
    context_builder,
    generation_service,
    planning_context_service,
    project_access,
    scene_service,
    setting_field_service,
    setting_progression_service,
)
from app.services import chapter_service as svc

router = APIRouter(tags=["chapters"])


@router.get("/projects/{project_id}/chapters", response_model=list[ChapterBrief])
async def list_chapters(project_id: int, user: CurrentUser, db: DbDep) -> list[ChapterBrief]:
    chapters = await svc.list_manual_chapters(db, user.id, project_id)
    return await svc.manual_chapter_views(db, user.id, project_id, chapters, full=False)


@router.post("/projects/{project_id}/chapters", response_model=ChapterOut, status_code=status.HTTP_201_CREATED)
async def create_chapter(project_id: int, body: ChapterCreate, user: CurrentUser, db: DbDep) -> ChapterOut:
    chapter = await svc.create_chapter(db, user.id, project_id, body)
    return (await svc.manual_chapter_views(db, user.id, project_id, [chapter], full=True))[0]


@router.post(
    "/projects/{project_id}/chapters/from-outlines",
    response_model=list[ChapterBrief],
    status_code=status.HTTP_201_CREATED,
)
async def create_from_outlines(
    project_id: int, body: ChaptersFromOutlines, user: CurrentUser, db: DbDep
) -> list[ChapterBrief]:
    chapters = await svc.create_from_outlines(db, user.id, project_id, body)
    return await svc.manual_chapter_views(db, user.id, project_id, chapters, full=False)


@router.get("/projects/{project_id}/chapters/{chapter_id}", response_model=ChapterOut)
async def get_chapter(project_id: int, chapter_id: int, user: CurrentUser, db: DbDep) -> ChapterOut:
    chapter = await svc.get_manual_chapter(db, user.id, project_id, chapter_id, "content.read")
    return (await svc.manual_chapter_views(db, user.id, project_id, [chapter], full=True))[0]


@router.get("/projects/{project_id}/chapters/{chapter_id}/setting-context", response_model=SettingContextOut)
async def get_setting_context(project_id: int, chapter_id: int, user: CurrentUser, db: DbDep) -> SettingContextOut:
    with db.no_autoflush:
        chapter = await svc.get_manual_chapter(db, user.id, project_id, chapter_id, "content.read")
        content_revision = chapter.content_revision
        project = await project_access.require_project(db, user.id, project_id, "content.read")
        planning = await planning_context_service.load_planning_context(db, project, "chapter", chapter.id)
        text = await context_builder.build_setting_context(
            db, project, chapter, planning=planning, include_protected=True,
        )
        unverified_count = await setting_progression_service.count_unverified_timelines(
            db, user.id, project_id, manual=True,
        )
        await planning_context_service.check_planning_revision(db, project_id, planning.revision)
        if planning.fields_revision is not None:
            await setting_field_service.check_fields_revision(db, project_id, planning.fields_revision)
        if planning.setting_scope is not None:
            await planning.setting_scope.scope.verify(db)
        await scene_service.check_revision(db, chapter, content_revision)
        warnings = [f"该作品有 {unverified_count} 条旧设定尚未校准，早期设定可能不准确。"] if unverified_count else []
        return SettingContextOut(
            position=StoryPosition(chapter_id=chapter.id, chapter_no=chapter.chapter_no, boundary="start"),
            text=text,
            legacy_unverified_count=unverified_count,
            warnings=warnings,
        )


@router.patch("/projects/{project_id}/chapters/{chapter_id}", response_model=ChapterOut)
async def update_chapter(
    project_id: int, chapter_id: int, body: ChapterUpdate, user: CurrentUser, db: DbDep
) -> ChapterOut:
    chapter = await svc.update_chapter(db, user.id, project_id, chapter_id, body, manual=True)
    return (await svc.manual_chapter_views(db, user.id, project_id, [chapter], full=True))[0]


@router.delete("/projects/{project_id}/chapters/{chapter_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chapter(project_id: int, chapter_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_chapter(db, user.id, project_id, chapter_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/projects/{project_id}/chapters/{chapter_id}/cast", response_model=ChapterCastOut)
async def get_cast(project_id: int, chapter_id: int, user: CurrentUser, db: DbDep) -> ChapterCastOut:
    return ChapterCastOut(character_ids=await cast_service.get_cast(db, user.id, project_id, chapter_id, manual=True))


@router.put("/projects/{project_id}/chapters/{chapter_id}/cast", response_model=ChapterCastOut)
async def set_cast(
    project_id: int, chapter_id: int, body: ChapterCastUpdate, user: CurrentUser, db: DbDep
) -> ChapterCastOut:
    ids = await cast_service.set_cast(db, user.id, project_id, chapter_id, body.character_ids, manual=True)
    return ChapterCastOut(character_ids=ids)


@router.post("/chapters/{chapter_id}/generate", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def generate_chapter(
    chapter_id: int,
    user: CurrentUser,
    db: DbDep,
    engine: TaskEngineDep,
    body: GenerateChapterRequest | None = None,
) -> TaskOut:
    params = {"chapter_id": chapter_id, **(body or GenerateChapterRequest()).model_dump(mode="json")}
    return await generation_service.create_chapter_task(db, engine, user.id, "chapter.generate", params)


@router.post("/chapters/{chapter_id}/polish", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def polish_chapter(
    chapter_id: int,
    user: CurrentUser,
    db: DbDep,
    engine: TaskEngineDep,
    body: PolishChapterRequest | None = None,
) -> TaskOut:
    params = {"chapter_id": chapter_id, **(body or PolishChapterRequest()).model_dump(mode="json")}
    return await generation_service.create_chapter_task(db, engine, user.id, "chapter.polish", params)
