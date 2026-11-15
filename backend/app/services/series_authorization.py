"""系列协作者客户端作用域代次；不是授权凭据。"""

from hashlib import sha256
from uuid import UUID


def authorization_version(series_id: int, actor_id: int, invitation_id: str | None, revision: int | None) -> str:
    """仅由已确认的 accepted editor 关系生成，不公开原始邀请身份。"""
    try:
        valid = (
            type(series_id) is int and series_id > 0 and type(actor_id) is int and actor_id > 0
            and type(revision) is int and revision > 0
            and isinstance(invitation_id, str) and str(UUID(invitation_id)) == invitation_id
        )
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("系列协作身份不可用")
    raw = f"series-collaboration-v1:{series_id}:{actor_id}:{invitation_id}:{revision}".encode()
    return sha256(raw).hexdigest()
