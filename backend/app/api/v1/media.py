import re
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.core.deps import DbDep, SettingsDep, get_current_user
from app.core.errors import UnauthorizedError
from app.media import access, service, signing
from app.models import User
from app.models.media import MediaObject

router = APIRouter(prefix="/media", tags=["media"])
RangeHeader = Annotated[str | None, Header(alias="Range")]


def _byte_range(header: str, size: int) -> tuple[int, int]:
    # 服务端只转发一段，浏览器拖动与后缀读取均使用单个字节区间。
    match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", header.strip(), re.IGNORECASE)
    if match:
        left, right = match.groups()
        if left:
            start = int(left)
            end = min(int(right), size - 1) if right else size - 1
        else:
            start = max(0, size - int(right or "0"))
            end = size - 1
        if 0 <= start <= end < size:
            return start, end
    headers = {"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"}
    raise HTTPException(416, "请求的字节范围无效", headers=headers)


async def _media_user(request: Request, db: DbDep, settings: SettingsDep) -> User:
    try:
        return await get_current_user(request, db, settings)
    except UnauthorizedError:
        raise access.missing() from None


class _MediaResponse(StreamingResponse):
    """先打开后端对象，再发响应头；客户端中途断开也会释放文件或 S3 连接。"""

    def __init__(self, stream: AsyncGenerator[bytes, None], first: bytes | None, **kwargs):
        self._stream = stream

        async def chunks():
            if first is not None:
                yield first
            async for chunk in stream:
                yield chunk

        super().__init__(chunks(), **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._stream.aclose()


async def _response(db: DbDep, obj: MediaObject, range_header: str | None, *, download: bool) -> StreamingResponse:
    size = obj.size_bytes
    headers = {
        "Cache-Control": "private, no-store", "Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "attachment" if download else "inline",
    }
    start = end = None
    if range_header is not None:
        start, end = _byte_range(range_header, size)
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(size if start is None else end - start + 1)
    mime = obj.mime
    stream = await service.open_range(db, obj, start, end)
    # 后端流已持有位置和键，慢速播放不再占用数据库连接。
    await db.rollback()
    try:
        first = await anext(stream, None)
    except FileNotFoundError:
        raise access.missing() from None
    return _MediaResponse(stream, first, status_code=200 if start is None else 206, media_type=mime, headers=headers)


@router.get("/external/{token}")
async def read_external(token: str, db: DbDep, range_header: RangeHeader = None):
    object_id = signing.verify(token, db.info["media_settings"])
    obj = await access.resolve(db, object_id, actor_id=None)
    return await _response(db, obj, range_header, download=False)


@router.get("/{object_id}")
async def read_media(
    object_id: str, user: Annotated[User, Depends(_media_user)], db: DbDep,
    variant: access.Variant | None = None, download: bool = False, range_header: RangeHeader = None,
):
    obj = await access.resolve(db, object_id, actor_id=user.id, variant=variant)
    return await _response(db, obj, range_header, download=download)
