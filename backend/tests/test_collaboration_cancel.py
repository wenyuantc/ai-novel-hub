"""请求取消时仍回滚或核实审计，并保持调用方的取消语义。"""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CollaborationAuditEvent, ProjectCollaborator
from app.services import collaboration_service
from tests.helpers import create_project, register


@pytest.mark.parametrize("committed", [False, True])
async def test_repeated_cancellation_preserves_audit_result(app, client, make_client, monkeypatch, committed):
    await register(client, "cancel-owner")
    guest = await register(make_client(), "cancel-guest")
    project = await create_project(client)
    entered, cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    never = asyncio.Event()
    original_add = collaboration_service.add_success
    original_commit = AsyncSession.commit
    original_failure = collaboration_service.record_failure

    async def gated_add(db, snapshot):
        if committed:
            result = await original_add(db, snapshot)
            db.info["cancel_after_commit"] = True
            return result
        entered.set()
        await never.wait()

    async def gated_commit(db):
        await original_commit(db)
        if db.info.pop("cancel_after_commit", False):
            entered.set()
            await never.wait()

    async def gated_failure(*args, **kwargs):
        cleanup_entered.set()
        await cleanup_release.wait()
        return await original_failure(*args, **kwargs)

    monkeypatch.setattr(collaboration_service, "add_success", gated_add)
    monkeypatch.setattr(AsyncSession, "commit", gated_commit)
    monkeypatch.setattr(collaboration_service, "record_failure", gated_failure)
    pending = asyncio.create_task(client.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "cancel-guest", "role": "viewer",
    }))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        await asyncio.wait_for(cleanup_entered.wait(), 5)
        pending.cancel()  # 重复取消不能穿透清理任务。
        await asyncio.sleep(0)
        assert not pending.done()
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 5)
    finally:
        cleanup_release.set()
        if not pending.done():
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(ProjectCollaborator.user_id == guest["id"]))
        events = list(await db.scalars(select(CollaborationAuditEvent).where(
            CollaborationAuditEvent.target_user_id == guest["id"],
        )))
        assert len(events) == 1
        if committed:
            assert member is not None and member.status == "pending"
            assert events[0].result == "success"
        else:
            assert member is None
            assert events[0].result == "failure" and events[0].error_code == "cancelled"


async def test_cancel_remains_cancelled_if_independent_audit_fails(app, client, make_client, monkeypatch, caplog):
    await register(client, "cancel-fail-owner")
    guest = await register(make_client(), "cancel-fail-guest")
    project = await create_project(client)
    entered = asyncio.Event()
    never = asyncio.Event()

    async def blocked_add(db, snapshot):
        entered.set()
        await never.wait()

    async def unavailable_audit(*args, **kwargs):
        raise ConnectionError("internal-private-error")

    monkeypatch.setattr(collaboration_service, "add_success", blocked_add)
    monkeypatch.setattr(collaboration_service, "record_failure", unavailable_audit)
    pending = asyncio.create_task(client.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "cancel-fail-guest", "role": "viewer",
    }))
    await asyncio.wait_for(entered.wait(), 5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 5)
    assert "internal-private-error" not in caplog.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(ProjectCollaborator.id).where(ProjectCollaborator.user_id == guest["id"])) is None
        assert await db.scalar(select(CollaborationAuditEvent.id)) is None


async def test_cancellation_during_initial_invitation_lookup_is_audited(app, client, make_client, monkeypatch):
    await register(client, "cancel-lookup-owner")
    guest_client = make_client()
    guest = await register(guest_client, "cancel-lookup-guest")
    project = await create_project(client)
    invited = await client.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "cancel-lookup-guest", "role": "viewer",
    })
    assert invited.status_code == 201
    entered, never = asyncio.Event(), asyncio.Event()
    original_scalar = AsyncSession.scalar
    intercepted = False

    async def blocked_lookup(db, statement, *args, **kwargs):
        nonlocal intercepted
        columns = getattr(statement, "column_descriptions", [])
        if not intercepted and any(item.get("entity") is ProjectCollaborator for item in columns):
            intercepted = True
            entered.set()
            await never.wait()
        return await original_scalar(db, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", blocked_lookup)
    path = f"/api/v1/collaboration/invitations/project/{invited.json()['invitation_id']}/accept"
    pending = asyncio.create_task(guest_client.post(path, json={"expected_revision": 1}))
    await asyncio.wait_for(entered.wait(), 5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 5)
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(ProjectCollaborator.user_id == guest["id"]))
        event = await db.scalar(select(CollaborationAuditEvent).where(
            CollaborationAuditEvent.action == "accept", CollaborationAuditEvent.target_user_id == guest["id"],
        ))
        assert member.status == "pending"
        assert (event.result, event.resource_id, event.error_code) == ("failure", 0, "cancelled")


@pytest.mark.parametrize("audit_fails", [False, True])
async def test_cancellation_during_failed_operation_audit_is_not_lost(app, client, monkeypatch, audit_fails):
    owner = await register(client, "cancel-failed-owner")
    project = await create_project(client)
    entered, release = asyncio.Event(), asyncio.Event()
    original_failure = collaboration_service.record_failure

    async def gated_failure(*args, **kwargs):
        entered.set()
        await release.wait()
        if audit_fails:
            raise ConnectionError("failed audit")
        return await original_failure(*args, **kwargs)

    monkeypatch.setattr(collaboration_service, "record_failure", gated_failure)
    pending = asyncio.create_task(client.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "cancel-failed-owner", "role": "viewer",
    }))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 5)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(ProjectCollaborator.id).where(ProjectCollaborator.user_id == owner["id"])) is None
        events = list(await db.scalars(select(CollaborationAuditEvent)))
        if audit_fails:
            assert events == []
        else:
            assert len(events) == 1 and events[0].error_code == "self_invite" and events[0].result == "failure"
