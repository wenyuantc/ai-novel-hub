import asyncio
import json
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import select

from app.models import AdminAuditLog
from app.services.publication_terms import effective_terms
from tests.helpers import register
from tests.test_task_worker_migration import migration_db as migration_db

PERSONAL = "/api/v1/publication-terms"
SITE = "/api/v1/admin/publication-terms"


async def test_publication_terms_crud_scope_and_audit(app, client, make_client, system_admin):
    author = await register(client, "核对作者")
    other = make_client()
    editor = await register(other, "核对编辑")
    empty = {"terms": [], "revision": 0}
    assert (await client.get(PERSONAL)).json() == empty
    assert (await system_admin.get(SITE)).json() == empty
    assert (await client.get(SITE)).status_code == 403
    assert (await client.put(SITE, json={"terms": ["越权"], "expected_revision": 0})).status_code == 403
    assert (await system_admin.put(SITE, json={"terms": [" 共有 ", "全站"], "expected_revision": 0})).json() == {
        "terms": ["共有", "全站"], "revision": 1,
    }
    valid = {"terms": ["共有", "私人"], "expected_revision": 0}
    assert (await client.put(PERSONAL, json=valid)).json() == {"terms": valid["terms"], "revision": 1}
    assert (await client.put(PERSONAL, json=valid)).status_code == 409
    assert (await other.get(PERSONAL, params={"owner_id": author["id"]})).json() == empty
    assert (await other.put(PERSONAL, json={**valid, "owner_id": author["id"]})).status_code == 422
    headers = {"X-Editor-Actor-Id": str(author["id"])}
    assert (await other.get(PERSONAL, headers=headers)).status_code == 409
    assert (await other.put(PERSONAL, headers=headers, json=valid)).status_code == 409
    assert (await system_admin.put(SITE, headers=headers, json=valid)).status_code == 409
    assert (await other.put(PERSONAL, json={"terms": ["编辑"], "expected_revision": 0})).is_success

    async with app.state.sessionmaker() as db:
        assert await effective_terms(db, author["id"]) == (1, 1, ["共有", "全站", "私人"])
        assert await effective_terms(db, editor["id"]) == (1, 1, ["共有", "全站", "编辑"])
        log = (await db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "publication_terms.update"))).one()
        assert log.result == "success" and log.target_type == "publication_terms"
        assert "共有" not in json.dumps(log.summary, ensure_ascii=False)

    for terms in ([""], [" 空", "空 "], ["断\x00句"], ["字" * 129], [str(i) for i in range(1001)]):
        assert (await client.put(PERSONAL, json={"terms": terms, "expected_revision": 1})).status_code == 422
    for revision, terms in [(1, ["新词"]), (2, [])]:
        saved = await client.put(PERSONAL, json={"terms": terms, "expected_revision": revision})
        assert saved.json() == {"terms": terms, "revision": revision + 1}
    assert (await client.put(PERSONAL, json={"terms": [], "expected_revision": 3})).json()["revision"] == 3


async def test_downgrade_preserves_audit_of_previously_removed_permission(migration_db):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0076")
    async with engine.begin() as db:
        await db.execute(AdminAuditLog.__table__.insert().values(
            operation_id=str(uuid4()), actor_username="管理员", action="role.update",
            result="success", target_type="role",
            summary={"rbac_change": {"permissions_before": ["publication_terms.read"], "permissions_after": []}},
        ))
    with pytest.raises(RuntimeError, match="授权审计"):
        await asyncio.to_thread(command.downgrade, cfg, "0075")
    async with engine.begin() as db:
        assert await db.scalar(select(AdminAuditLog.action)) == "role.update"
