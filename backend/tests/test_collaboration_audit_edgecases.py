"""邀请审计处理连接异常与账号状态变化时保留真实结果。"""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import UnauthorizedError
from app.models import CollaborationAuditEvent, ProjectCollaborator, User
from app.services import collaboration_service
from tests.helpers import create_project, register


@pytest.mark.parametrize("invalidate_fails, lookup_unavailable", [(False, False), (True, False), (True, True)])
async def test_successful_commit_is_reconciled_when_rollback_also_fails(
    app, client, make_client, monkeypatch, invalidate_fails, lookup_unavailable,
):
    await register(client, "audit-loss-owner")
    guest = await register(make_client(), "audit-loss-guest")
    project = await create_project(client)
    original_add = collaboration_service.add_success
    original_commit = AsyncSession.commit
    original_rollback = AsyncSession.rollback
    original_invalidate = AsyncSession.invalidate

    async def mark(db, snapshot):
        event = await original_add(db, snapshot)
        db.info["simulate_lost_commit"] = True
        return event

    async def lost_commit(db):
        await original_commit(db)
        if db.info.pop("simulate_lost_commit", False):
            db.info["simulate_failed_rollback"] = True
            raise ConnectionError("提交确认丢失")

    async def failed_rollback(db):
        if db.info.pop("simulate_failed_rollback", False):
            db.info["simulate_failed_invalidation"] = invalidate_fails
            raise ConnectionError("连接断开，回滚不可用")
        return await original_rollback(db)

    async def failed_invalidate(db):
        if db.info.pop("simulate_failed_invalidation", False):
            raise ConnectionError("连接失效操作也不可用")
        return await original_invalidate(db)

    async def unavailable_lookup(*args):
        raise ConnectionError("审计库暂不可用")

    async def forbidden_failure_write(*args):
        pytest.fail("原事务状态未确认时不能补写失败审计")

    monkeypatch.setattr(collaboration_service, "add_success", mark)
    monkeypatch.setattr(AsyncSession, "commit", lost_commit)
    monkeypatch.setattr(AsyncSession, "rollback", failed_rollback)
    monkeypatch.setattr(AsyncSession, "invalidate", failed_invalidate)
    if lookup_unavailable:
        monkeypatch.setattr(collaboration_service, "get_result", unavailable_lookup)
        monkeypatch.setattr(collaboration_service, "record_failure", forbidden_failure_write)
    response = await client.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "audit-loss-guest", "role": "viewer",
    })
    if lookup_unavailable:
        assert response.status_code == 500 and response.json()["detail"] == "协作审计暂不可用，请稍后重试"
    else:
        assert response.status_code == 409 and response.json()["code"] == "operation_confirmed"
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(ProjectCollaborator.user_id == guest["id"]))
        events = list(await db.scalars(select(CollaborationAuditEvent).where(
            CollaborationAuditEvent.target_user_id == guest["id"],
        )))
        assert member.status == "pending"
        assert len(events) == 1 and events[0].result == "success"


async def test_expired_actor_is_recorded_as_unauthorized(app, client, make_client, monkeypatch):
    await register(client, "audit-expiry-owner")
    guest = await register(make_client(), "audit-expiry-guest")
    project = await create_project(client)
    original = collaboration_service.authz.require_authenticated

    async def expire_before_write(db, actor, *, write=False, targets=()):
        if write:
            raise UnauthorizedError("未登录或登录已过期")
        return await original(db, actor, write=write, targets=targets)

    monkeypatch.setattr(collaboration_service.authz, "require_authenticated", expire_before_write)
    response = await client.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "audit-expiry-guest", "role": "viewer",
    })
    assert response.status_code == 401
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(ProjectCollaborator.id).where(ProjectCollaborator.user_id == guest["id"])) is None
        event = await db.scalar(select(CollaborationAuditEvent).where(
            CollaborationAuditEvent.target_user_id == guest["id"],
        ))
        assert event.result == "failure" and event.error_code == "unauthorized"


@pytest.mark.parametrize("account_change", ["disable", "revoke_token"])
async def test_invalid_actor_cannot_respond_to_unknown_invitation(app, client, monkeypatch, account_change):
    actor = await register(client, "disabled-before-response")
    original = collaboration_service._write

    async def disable_after_authentication(db, factory, current_user, *args, **kwargs):
        async with factory() as other:
            user = await other.get(User, current_user.id)
            if account_change == "disable":
                user.is_active = False
            else:
                user.token_version += 1
            await other.commit()
        return await original(db, factory, current_user, *args, **kwargs)

    monkeypatch.setattr(collaboration_service, "_write", disable_after_authentication)
    response = await client.post(
        f"/api/v1/collaboration/invitations/project/{uuid4()}/accept", json={"expected_revision": 1},
    )
    assert response.status_code == 401
    async with app.state.sessionmaker() as db:
        event = await db.scalar(select(CollaborationAuditEvent).where(
            CollaborationAuditEvent.actor_user_id == actor["id"], CollaborationAuditEvent.action == "accept",
        ))
        assert event.result == "failure" and event.error_code == "unauthorized" and event.resource_id == 0
