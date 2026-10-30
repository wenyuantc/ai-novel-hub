"""仅通过网页登录管理本人平台 API Key。"""

from fastapi import APIRouter, Response, status

from app.core.deps import CipherDep, CurrentUser, DbDep
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreateOut, ApiKeyOut, ApiKeyRevealOut, ApiKeyUpdate
from app.services import api_key_service as svc

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(user: CurrentUser, db: DbDep, response: Response) -> list[ApiKeyOut]:
    response.headers["Cache-Control"] = "no-store"
    return await svc.list_keys(db, user.id)


@router.post("", response_model=ApiKeyCreateOut, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: ApiKeyCreate, user: CurrentUser, db: DbDep, cipher: CipherDep, response: Response
) -> ApiKeyCreateOut:
    response.headers["Cache-Control"] = "no-store"
    return await svc.create_key(db, user.id, cipher, body)


@router.patch("/{key_id}", response_model=ApiKeyOut)
async def update_key(key_id: int, body: ApiKeyUpdate, user: CurrentUser, db: DbDep, response: Response) -> ApiKeyOut:
    response.headers["Cache-Control"] = "no-store"
    return await svc.update_key(db, user.id, key_id, body)


@router.post("/{key_id}/reveal", response_model=ApiKeyRevealOut)
async def reveal_key(
    key_id: int, user: CurrentUser, db: DbDep, cipher: CipherDep, response: Response
) -> ApiKeyRevealOut:
    response.headers["Cache-Control"] = "no-store"
    return ApiKeyRevealOut(key=await svc.reveal_key(db, user.id, key_id, cipher))


@router.post("/{key_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(key_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.revoke_key(db, user.id, key_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})
