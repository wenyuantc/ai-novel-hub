from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Response

from app.core.deps import CurrentUser, DbDep, EditorUser
from app.plugins.drama import episode_schemas as schema
from app.plugins.drama.services import episode_models, episodes, export, shots
from app.plugins.drama.styles import STYLES

router = APIRouter(prefix="/plugins/drama", tags=["短剧"])
ResourceId = Annotated[int, Path(ge=1, le=2_147_483_647)]
Revision = Annotated[int, Query(ge=0)]


@router.get("/styles")
async def list_styles(user: CurrentUser):
    return [{"key": key, "label": label} for key, label in STYLES.items()]


@router.get("/projects/{pid}/series", response_model=list[schema.SeriesOut])
async def list_series(pid: ResourceId, user: CurrentUser, db: DbDep):
    return await episodes.list_series(db, user.id, pid)


@router.post("/projects/{pid}/series", response_model=schema.SeriesOut, status_code=201)
async def create_series(pid: ResourceId, body: schema.SeriesCreate, user: EditorUser, db: DbDep):
    return await episodes.create_series(db, user.id, pid, body)


@router.get("/series/{sid}", response_model=schema.SeriesOut)
async def get_series(sid: ResourceId, user: CurrentUser, db: DbDep):
    return await episodes.get_series(db, user.id, sid)


@router.patch("/series/{sid}", response_model=schema.SeriesOut)
async def patch_series(sid: ResourceId, body: schema.SeriesPatch, user: EditorUser, db: DbDep):
    return await episodes.update_series(db, user.id, sid, body)


@router.delete("/series/{sid}", status_code=204)
async def delete_series(sid: ResourceId, revision: Revision, user: EditorUser, db: DbDep):
    await episodes.delete_series(db, user.id, sid, revision)
    return Response(status_code=204)


@router.get("/series/{sid}/episodes", response_model=list[schema.EpisodeSummary])
async def list_episodes(sid: ResourceId, user: CurrentUser, db: DbDep):
    return await episodes.list_episodes(db, user.id, sid)


@router.post("/series/{sid}/episodes", response_model=schema.EpisodeOut, status_code=201)
async def create_episode(sid: ResourceId, body: schema.EpisodeCreate, user: EditorUser, db: DbDep):
    return await episodes.create_episode(db, user.id, sid, body)


@router.get("/episodes/{eid}", response_model=schema.EpisodeOut)
async def get_episode(eid: ResourceId, user: CurrentUser, db: DbDep):
    return await episodes.read_episode(db, user.id, eid)


@router.patch("/episodes/{eid}", response_model=schema.EpisodeOut)
async def patch_episode(eid: ResourceId, body: schema.EpisodePatch, user: EditorUser, db: DbDep):
    return await episodes.update_episode(db, user.id, eid, body)


@router.delete("/episodes/{eid}", status_code=204)
async def delete_episode(eid: ResourceId, revision: Revision, user: EditorUser, db: DbDep):
    await episodes.delete_episode(db, user.id, eid, revision)
    return Response(status_code=204)


@router.get("/episodes/{eid}/models", response_model=schema.EpisodeModels)
async def read_episode_models(eid: ResourceId, user: EditorUser, db: DbDep, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    return await episode_models.read_models(db, user.id, eid)


@router.patch("/episodes/{eid}/models", response_model=schema.EpisodeModels)
async def update_episode_models(
    eid: ResourceId, body: schema.EpisodeModels, user: EditorUser, db: DbDep, response: Response,
):
    response.headers["Cache-Control"] = "private, no-store"
    return await episode_models.update_models(db, user.id, eid, body)


@router.get("/episodes/{eid}/shots", response_model=list[schema.ShotOut])
async def list_shots(eid: ResourceId, user: CurrentUser, db: DbDep):
    return await shots.list_shots(db, user.id, eid)


@router.post("/episodes/{eid}/shots", response_model=schema.ShotOut, status_code=201)
async def create_shot(eid: ResourceId, body: schema.ShotCreate, user: EditorUser, db: DbDep):
    return await shots.create_shot(db, user.id, eid, body)


@router.patch("/shots/{shot_id}", response_model=schema.ShotOut)
async def patch_shot(shot_id: ResourceId, body: schema.ShotPatch, user: EditorUser, db: DbDep):
    return await shots.update_shot(db, user.id, shot_id, body)


@router.delete("/shots/{shot_id}", status_code=204)
async def delete_shot(shot_id: ResourceId, revision: Revision, user: EditorUser, db: DbDep):
    await shots.delete_shot(db, user.id, shot_id, revision)
    return Response(status_code=204)


@router.put("/episodes/{eid}/shots/order", response_model=list[schema.ShotOut])
async def order_shots(eid: ResourceId, body: schema.ShotOrder, user: EditorUser, db: DbDep):
    return await shots.order_shots(db, user.id, eid, body)


@router.get("/episodes/{eid}/export")
async def export_episode(
    eid: ResourceId,
    user: CurrentUser,
    db: DbDep,
    format: Literal["md", "docx"] = "docx",
    parts: str = "script,shots",
):
    attachment = await export.export_episode(db, user.id, eid, format, parts)
    headers = {"Content-Disposition": attachment.content_disposition, "Cache-Control": "no-store"}
    if attachment.warnings_header:
        headers["X-Export-Warnings"] = attachment.warnings_header
    return Response(content=attachment.body, media_type=attachment.media_type, headers=headers)
