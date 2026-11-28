"""剧级素材维护，以及素材与剧集的关联。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Response

from app.core.deps import CurrentUser, DbDep, EditorUser
from app.plugins.drama import asset_schemas as schema
from app.plugins.drama.services import assets
from app.plugins.drama.services.episodes import get_episode, get_series

router = APIRouter(prefix="/plugins/drama", tags=["短剧"])
Resource = Annotated[int, Path(gt=0, le=2_147_483_647)]
Expected = Annotated[int, Query(ge=0, le=2_147_483_647)]
EpisodeId = Annotated[int | None, Query(gt=0, le=2_147_483_647)]
EpisodeRevision = Annotated[int | None, Query(ge=0, le=2_147_483_647)]
MemberKind = Literal["looks", "scenes", "props"]


@router.get("/series/{sid}/assets", response_model=schema.AssetsOut)
async def series_assets(sid: Resource, db: DbDep, user: CurrentUser):
    series = await get_series(db, user.id, sid)
    return await assets.library(db, series.id, include_images=True)


@router.get("/episodes/{eid}/assets", response_model=schema.AssetsOut)
async def episode_assets(eid: Resource, db: DbDep, user: CurrentUser):
    series, episode = await get_episode(db, user.id, eid)
    return await assets.library(db, series.id, episode.id, include_images=True)


async def _create(db, user, kind, parent_id, body, eid, revision):
    data = await assets.create_asset(
        db, user.id, kind, parent_id, body.model_dump(), episode_id=eid, episode_revision=revision
    )
    await db.commit()
    return data


async def _patch(db, user, kind, resource_id, body):
    data = await assets.update_asset(db, user.id, kind, resource_id, body.model_dump(exclude_unset=True))
    await db.commit()
    return data


async def _delete(db, user, kind, resource_id, revision):
    await assets.delete_asset(db, user.id, kind, resource_id, revision)
    await db.commit()
    return Response(status_code=204)


@router.post("/series/{parent_id}/characters", status_code=201, response_model=schema.CharacterOut)
async def create_character(
    parent_id: Resource,
    body: schema.CharacterCreate,
    user: EditorUser,
    db: DbDep,
    episode_id: EpisodeId = None,
    episode_revision: EpisodeRevision = None,
):
    return await _create(db, user, "characters", parent_id, body, episode_id, episode_revision)


@router.patch("/characters/{resource_id}", response_model=schema.CharacterOut)
async def patch_character(resource_id: Resource, body: schema.CharacterPatch, user: EditorUser, db: DbDep):
    return await _patch(db, user, "characters", resource_id, body)


@router.delete("/characters/{resource_id}", status_code=204)
async def delete_character(resource_id: Resource, revision: Expected, user: EditorUser, db: DbDep):
    return await _delete(db, user, "characters", resource_id, revision)


@router.post("/characters/{parent_id}/looks", status_code=201, response_model=schema.LookOut)
async def create_look(
    parent_id: Resource,
    body: schema.LookCreate,
    user: EditorUser,
    db: DbDep,
    episode_id: EpisodeId = None,
    episode_revision: EpisodeRevision = None,
):
    return await _create(db, user, "looks", parent_id, body, episode_id, episode_revision)


@router.patch("/looks/{resource_id}", response_model=schema.LookOut)
async def patch_look(resource_id: Resource, body: schema.LookPatch, user: EditorUser, db: DbDep):
    return await _patch(db, user, "looks", resource_id, body)


@router.delete("/looks/{resource_id}", status_code=204)
async def delete_look(resource_id: Resource, revision: Expected, user: EditorUser, db: DbDep):
    return await _delete(db, user, "looks", resource_id, revision)


@router.post("/series/{parent_id}/scenes", status_code=201, response_model=schema.SceneOut)
async def create_scene(
    parent_id: Resource,
    body: schema.SceneCreate,
    user: EditorUser,
    db: DbDep,
    episode_id: EpisodeId = None,
    episode_revision: EpisodeRevision = None,
):
    return await _create(db, user, "scenes", parent_id, body, episode_id, episode_revision)


@router.patch("/scenes/{resource_id}", response_model=schema.SceneOut)
async def patch_scene(resource_id: Resource, body: schema.ScenePatch, user: EditorUser, db: DbDep):
    return await _patch(db, user, "scenes", resource_id, body)


@router.delete("/scenes/{resource_id}", status_code=204)
async def delete_scene(resource_id: Resource, revision: Expected, user: EditorUser, db: DbDep):
    return await _delete(db, user, "scenes", resource_id, revision)


@router.post("/series/{parent_id}/props", status_code=201, response_model=schema.PropOut)
async def create_prop(
    parent_id: Resource,
    body: schema.PropCreate,
    user: EditorUser,
    db: DbDep,
    episode_id: EpisodeId = None,
    episode_revision: EpisodeRevision = None,
):
    return await _create(db, user, "props", parent_id, body, episode_id, episode_revision)


@router.patch("/props/{resource_id}", response_model=schema.PropOut)
async def patch_prop(resource_id: Resource, body: schema.PropPatch, user: EditorUser, db: DbDep):
    return await _patch(db, user, "props", resource_id, body)


@router.delete("/props/{resource_id}", status_code=204)
async def delete_prop(resource_id: Resource, revision: Expected, user: EditorUser, db: DbDep):
    return await _delete(db, user, "props", resource_id, revision)


@router.put("/episodes/{eid}/assets/{kind}/{resource_id}", status_code=204)
async def join_episode(
    eid: Resource, kind: MemberKind, resource_id: Resource, revision: Expected, user: EditorUser, db: DbDep
):
    await assets.set_membership(db, user.id, eid, kind, resource_id, revision, add=True)
    await db.commit()
    return Response(status_code=204)


@router.delete("/episodes/{eid}/assets/{kind}/{resource_id}", status_code=204)
async def leave_episode(
    eid: Resource, kind: MemberKind, resource_id: Resource, revision: Expected, user: EditorUser, db: DbDep
):
    await assets.set_membership(db, user.id, eid, kind, resource_id, revision, add=False)
    await db.commit()
    return Response(status_code=204)
