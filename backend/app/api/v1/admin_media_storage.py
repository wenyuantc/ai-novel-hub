from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.deps import DbDep, require_permission
from app.media import locations
from app.models import User
from app.schemas.media_storage import LocationInput, SavedLocation, StorageOut, StoragePatch
from app.services.authorization_service import AuthenticatedActor

router = APIRouter(prefix="/admin/media-storage", tags=["admin"])
ReadUser = Annotated[User, Depends(require_permission("media_storage.read"))]
ConfigureUser = Annotated[User, Depends(require_permission("media_storage.configure"))]


@router.get("")
async def read_settings(user: ReadUser, db: DbDep) -> StorageOut:
    return await locations.describe(db)


@router.post("/test")
async def test_storage(
    user: ConfigureUser, db: DbDep, body: LocationInput | SavedLocation | None = None
) -> dict[str, bool]:
    await locations.test_location(db, body)
    return {"ok": True}


@router.patch("")
async def update_settings(user: ConfigureUser, db: DbDep, body: StoragePatch) -> StorageOut:
    return await locations.apply(db, AuthenticatedActor(user.id, user.token_version), body)
