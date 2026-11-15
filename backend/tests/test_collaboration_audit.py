"""授权审计与业务同事务、失败独立记录且未知提交不造相反结果。"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import CollaborationAuditEvent, Project, ProjectCollaborator, User
from app.services.collaboration_audit import AuditSnapshot, add_success, record_failure
from tests.helpers import create_project, register


async def test_success_rolls_back_with_business_then_failure_is_independent(app, client, make_client):
    owner = await register(client, "audit-owner")
    guest = await register(make_client(), "audit-guest")
    project = await create_project(client)
    invitation = str(uuid4())
    event = AuditSnapshot(
        operation_id=str(uuid4()), scope="project", resource_id=project["id"], action="invite",
        actor_user_id=owner["id"], actor_username="audit-owner", target_user_id=guest["id"],
        target_username="audit-guest", invitation_id=invitation,
        summary={"after_role": "editor", "after_allow_owner_ai": False},
    )
    with pytest.raises(RuntimeError):
        async with app.state.sessionmaker() as db:
            db.add(ProjectCollaborator(
                project_id=project["id"], user_id=guest["id"], role="editor", invitation_id=invitation,
            ))
            await add_success(db, event)
            raise RuntimeError("simulate rollback")
    assert await record_failure(app.state.sessionmaker, event, "stale_invitation") == "failure"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(ProjectCollaborator.id)) is None
        audit = await db.scalar(select(CollaborationAuditEvent))
        assert audit.result == "failure" and audit.error_code == "stale_invitation"
        assert audit.summary == {}  # 未提交的 after 值不能被记录成事实。
        await db.delete(await db.get(Project, project["id"]))
        await db.delete(await db.get(User, guest["id"]))
        await db.delete(await db.get(User, owner["id"]))
        await db.commit()
        audit = await db.scalar(select(CollaborationAuditEvent))
        assert (audit.actor_user_id, audit.actor_username) == (owner["id"], "audit-owner")
        assert (audit.target_user_id, audit.target_username) == (guest["id"], "audit-guest")
        assert audit.resource_id == project["id"]


async def test_unknown_success_is_not_replaced_and_failure_is_idempotent(app, client):
    owner = await register(client, "audit-owner")
    project = await create_project(client)
    event = AuditSnapshot(
        operation_id=str(uuid4()), scope="project", resource_id=project["id"], action="revoke",
        actor_user_id=owner["id"], actor_username="audit-owner", summary={"before_role": "viewer"},
    )
    async with app.state.sessionmaker() as db:
        await add_success(db, event)
        await db.commit()
    assert await record_failure(app.state.sessionmaker, event, "system_error") == "success"
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(CollaborationAuditEvent)))
        assert len(rows) == 1 and rows[0].result == "success" and rows[0].error_code is None

    failure = AuditSnapshot(operation_id=str(uuid4()), scope="project", resource_id=project["id"], action="accept")
    assert await asyncio.gather(*[
        record_failure(app.state.sessionmaker, failure, "not_found") for _ in range(2)
    ]) == ["failure", "failure"]
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(CollaborationAuditEvent).where(
            CollaborationAuditEvent.operation_id == failure.operation_id
        )))
        assert len(rows) == 1 and rows[0].result == "failure"
        await db.delete(await db.get(Project, project["id"]))
        await db.delete(await db.get(User, owner["id"]))
        await db.commit()
        audit = await db.scalar(select(CollaborationAuditEvent).where(
            CollaborationAuditEvent.operation_id == event.operation_id
        ))
        assert audit.actor_user_id == owner["id"] and audit.actor_username == "audit-owner"
        assert audit.resource_id == project["id"]


async def test_failure_commit_confirmation_loss_is_reconciled(app):
    event = AuditSnapshot(operation_id=str(uuid4()), scope="project", resource_id=1, action="decline")
    real_maker = app.state.sessionmaker
    calls = 0

    class LostConfirmation:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            await self.session.__aenter__()
            return self

        async def __aexit__(self, *args):
            return await self.session.__aexit__(*args)

        def __getattr__(self, name):
            return getattr(self.session, name)

        async def commit(self):
            await self.session.commit()
            raise ConnectionError("模拟提交成功但确认丢失")

    def maker():
        nonlocal calls
        calls += 1
        session = real_maker()
        return LostConfirmation(session) if calls == 2 else session

    assert await record_failure(maker, event, "conflict") == "failure"
    async with real_maker() as db:
        rows = list(await db.scalars(select(CollaborationAuditEvent)))
        assert len(rows) == 1 and rows[0].error_code == "conflict"


async def test_unknown_resource_is_only_valid_for_failure(app):
    event = AuditSnapshot(operation_id=str(uuid4()), scope="project", resource_id=0, action="accept")
    async with app.state.sessionmaker() as db:
        with pytest.raises(ValueError):
            await add_success(db, event)
    assert await record_failure(app.state.sessionmaker, event, "not_found") == "failure"


def test_audit_summary_rejects_untrusted_text():
    with pytest.raises(ValueError):
        AuditSnapshot(operation_id=str(uuid4()), scope="project", resource_id=1, action="invite", summary={
            "prompt": "secret",
        })
