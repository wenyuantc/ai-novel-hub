from fastapi import APIRouter, Response, UploadFile, status

from app.core.deps import CipherDep, CurrentUser, DbDep, FactoryDep, SessionmakerDep, SettingsDep, TaskEngineDep
from app.schemas.ai_config import (
    AIConfigCreate,
    AIConfigOut,
    AIConfigTestResult,
    AIConfigUpdate,
    ModelListRequest,
    ModelListResult,
    VideoProbeRequest,
    VideoReferenceOut,
)
from app.schemas.task import TaskOut
from app.services import ai_config_service as svc
from app.services import task_service, video_probe_service

router = APIRouter(prefix="/ai-configs", tags=["ai-configs"])


@router.get("", response_model=list[AIConfigOut])
async def list_configs(user: CurrentUser, db: DbDep, cipher: CipherDep) -> list[AIConfigOut]:
    return [svc.to_out(c, cipher) for c in await svc.list_configs(db, user.id)]


@router.post("", response_model=AIConfigOut, status_code=status.HTTP_201_CREATED)
async def create_config(body: AIConfigCreate, user: CurrentUser, db: DbDep, cipher: CipherDep) -> AIConfigOut:
    return svc.to_out(await svc.create_config(db, cipher, user.id, body), cipher)


@router.post("/models", response_model=ModelListResult)
async def list_models(
    body: ModelListRequest, user: CurrentUser, db: DbDep, cipher: CipherDep, factory: FactoryDep, settings: SettingsDep
) -> ModelListResult:
    return await svc.list_models(db, cipher, factory, user.id, body, allow_private=settings.allow_private_ai_endpoints)


@router.get("/{config_id}", response_model=AIConfigOut)
async def get_config(config_id: int, user: CurrentUser, db: DbDep, cipher: CipherDep) -> AIConfigOut:
    return svc.to_out(await svc.get_config(db, user.id, config_id), cipher)


@router.patch("/{config_id}", response_model=AIConfigOut)
async def update_config(
    config_id: int, body: AIConfigUpdate, user: CurrentUser, db: DbDep, cipher: CipherDep
) -> AIConfigOut:
    return svc.to_out(await svc.update_config(db, cipher, user.id, config_id, body), cipher)


@router.delete("/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_config(config_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_config(db, user.id, config_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{config_id}/default", response_model=AIConfigOut)
async def set_default(config_id: int, user: CurrentUser, db: DbDep, cipher: CipherDep) -> AIConfigOut:
    return svc.to_out(await svc.set_default(db, user.id, config_id), cipher)


@router.post("/{config_id}/test", response_model=AIConfigTestResult)
async def test_config(
    config_id: int, user: CurrentUser, db: DbDep, cipher: CipherDep, factory: FactoryDep, settings: SettingsDep
) -> AIConfigTestResult:
    return await svc.test_config(
        db, cipher, factory, user.id, config_id, allow_private=settings.allow_private_ai_endpoints
    )


@router.post("/{config_id}/video-probe", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def start_video_probe(
    config_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep, body: VideoProbeRequest | None = None,
) -> TaskOut:
    params = {"config_id": config_id, "image_ids": [str(item) for item in body.image_ids] if body else []}
    return await task_service.create_task(db, engine, user.id, video_probe_service.KIND, params)


@router.get("/{config_id}/video-probe", response_model=TaskOut | None)
async def latest_video_probe(config_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut | None:
    return await video_probe_service.latest(db, engine, user.id, config_id)


@router.post("/{config_id}/video-probe/reference", response_model=VideoReferenceOut,
             status_code=status.HTTP_201_CREATED)
async def upload_video_reference(
    config_id: int, file: UploadFile, user: CurrentUser, db: DbDep, sessionmaker: SessionmakerDep,
) -> VideoReferenceOut:
    oid = await video_probe_service.store_reference(db, sessionmaker, user.id, config_id, file)
    return VideoReferenceOut(object_id=oid)
