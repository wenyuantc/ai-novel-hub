"""发布检查取词和维护都经过既有事务级授权门禁，空词表的首次写入也受保护。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import PublicationTerms
from app.schemas.publication_terms import PublicationTermsOut, PublicationTermsUpdate
from app.services import authorization_service as authz
from app.services.audit_writer import AuditOperation

GLOBAL = 0


async def read_terms(db: AsyncSession, scope_id: int) -> PublicationTermsOut:
    row = await db.get(PublicationTerms, scope_id, populate_existing=True)
    return PublicationTermsOut(terms=row.terms, revision=row.revision) if row else PublicationTermsOut(
        terms=[], revision=0,
    )


async def effective_terms(db: AsyncSession, owner_id: int) -> tuple[int, int, list[str]]:
    """调用者从已授权的作品解析 owner_id；与定稿或登记共用同一事务。"""
    await authz.gate(db)
    statement = select(PublicationTerms).where(PublicationTerms.id.in_([GLOBAL, owner_id]))
    rows = {row.id: row for row in await db.scalars(statement.execution_options(populate_existing=True))}
    site, author = rows.get(GLOBAL), rows.get(owner_id)
    terms = dict.fromkeys(term for row in (site, author) if row for term in row.terms)
    return site.revision if site else 0, author.revision if author else 0, list(terms)


async def replace_terms(
    db: AsyncSession, actor_id: int, body: PublicationTermsUpdate, *, global_scope: bool = False,
) -> PublicationTermsOut:
    scope_id = GLOBAL if global_scope else actor_id
    async with AuditOperation(db, actor_id, "publication_terms.update", "publication_terms",
                              enabled=global_scope) as audit:
        if global_scope:
            await authz.require(db, actor_id, "publication_terms.update", write=True)
            audit.values["target_label"] = "全站发布检查词表"
        else:
            await authz.require_authenticated(db, actor_id, write=True)
        row = await db.get(PublicationTerms, scope_id, populate_existing=True)
        revision = row.revision if row else 0
        if body.expected_revision != revision:
            raise ConflictError("词表已更新，请重新加载后再修改", extra={
                "code": "stale_publication_terms", "current_revision": revision,
            })
        if body.terms != (row.terms if row else []):
            revision += 1
            if row is None:
                row = PublicationTerms(id=scope_id, owner_id=None if global_scope else actor_id)
                db.add(row)
            row.terms = body.terms
            row.revision = revision
        await audit.commit()
    return PublicationTermsOut(terms=body.terms, revision=revision)
