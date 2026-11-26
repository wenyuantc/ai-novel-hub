"""定稿与登记复用的原稿核对，位置按 Unicode 码点计数。"""

import hmac
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Project, SerializationEdition, User
from app.schemas.serialization import FinalizeRequest, PublishRequest
from app.services.audit_writer import AuditOperation
from app.services.publication_term_matching import PAGE_SIZE, hit_page
from app.services.publication_terms import effective_terms
from app.services.serialization_snapshot import FrozenDraft


async def require_confirmation(
    db: AsyncSession, *, actor_id: int, project_id: int, chapter_id: int,
    body: FinalizeRequest | PublishRequest, manuscript: FrozenDraft | SerializationEdition,
    signing_secret: str, offset: int | None, audit: AuditOperation,
) -> None:
    # 调用者已经在同一事务内锁定并校验作品、稿件及编辑权限。
    owner_id = (await db.execute(select(Project.user_id).where(Project.id == project_id))).scalar_one()
    global_revision, personal_revision, terms = await effective_terms(db, owner_id)
    supplied = body.confirmation_token
    if not terms and supplied is None and offset is None:
        return

    actor = await db.get(User, actor_id)
    action = "finalize" if isinstance(body, FinalizeRequest) else "publish"
    baseline = [action, actor_id, actor.created_at.isoformat(), actor.token_version, project_id, chapter_id,
                body.model_dump(mode="json", exclude={"confirmation_token"}), manuscript.fingerprint,
                owner_id, global_revision, personal_revision, terms]
    canonical = json.dumps(baseline, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    signed_bytes = b"ainovel/publication-preflight/v1\x00" + canonical
    signature = hmac.digest(signing_secret.encode(), signed_bytes, "sha256").hex()
    matched = supplied is not None and hmac.compare_digest(supplied, signature)
    if matched and offset is None:
        return
    total, hits = hit_page(manuscript.title, manuscript.content, terms, offset or 0)
    if not total and supplied is None and offset is None:
        return
    stale = supplied is not None and not matched
    if not stale or offset is not None:
        # 等待用户核对和翻页是正常的只读结果。失效凭据的写入尝试仍由原有审计记录。
        audit.enabled = False
    raise ConflictError(
        "核对依据已变化，请重新核对" if stale else "请核对本次稿件的词条命中",
        extra={
            "code": "stale_serialization_preflight" if stale else "serialization_preflight_required",
            "preflight": {
                "confirmation_token": signature, "action": action, "chapter_id": chapter_id,
                "edition_id": body.edition_id if isinstance(body, PublishRequest) else None,
                "fingerprint": manuscript.fingerprint, "global_revision": global_revision,
                "personal_revision": personal_revision, "total": total, "offset": offset or 0,
                "limit": PAGE_SIZE, "hits": hits,
            },
        },
    )
