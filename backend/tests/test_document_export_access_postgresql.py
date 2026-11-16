"""导出最终授权快照在隔离 PostgreSQL 上的回归。"""

import asyncio

import httpx
import pytest
from sqlalchemy import update

from app.core.errors import AppError
from app.models import AgentConversation, AgentMessage, User
from app.services.agent import export_reads
from app.services.authorization_service import AuthenticatedActor
from tests import pg_support
from tests.helpers import create_project, register
from tests.test_agent_entries_e4id import _invite, _revoke


@pytest.fixture
async def postgres_export_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(purpose="文档导出授权测试")
    async with pg_support.postgres_apps(pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2) as apps:
        yield apps


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _world(app, owner_client, member_client, *, message_count=1):
    owner = await register(owner_client, "document-owner")
    member = await register(member_client, "document-viewer")
    project = await create_project(owner_client)
    grant = await _invite(owner_client, member_client, project["id"], "document-viewer", role="viewer")
    async with app.state.sessionmaker() as db:
        user = await db.get(User, member["id"])
        actor = AuthenticatedActor(user.id, user.token_version)
        # 无当前邀请标记的旧会话，即使 writable 一直为 false 仍可读取。
        conversation = AgentConversation(user_id=member["id"], project_id=project["id"], title="旧轮次")
        db.add(conversation)
        await db.flush()
        db.add_all([AgentMessage(conversation_id=conversation.id, role="user", content=f"消息 {i}")
                    for i in range(message_count)])
        await db.commit()
        return owner, member, project, grant, actor, conversation.id


async def _capture(app, actor, project_id, conversation_id):
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        capture = await export_reads.capture_document_access(db, actor.id, project_id, [conversation_id])
        await db.rollback()
        await export_reads.verify_document_access(db, capture)
        return capture


async def _verify(app, actor, capture):
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        await export_reads.verify_document_access(db, capture)


async def test_postgresql_large_chat_and_reinvitation_invalidates_capture(postgres_export_apps):
    app = postgres_export_apps[0]
    async with _client(app) as owner, _client(app) as member:
        _owner, user, project, grant, actor, conversation_id = await _world(app, owner, member, message_count=451)
        capture = await _capture(app, actor, project["id"], conversation_id)
        assert len(capture.conversations[0]["messages"]) == 451
        await _revoke(owner, project["id"], user["id"], grant["revision"])
        await _invite(owner, member, project["id"], "document-viewer", role="viewer")
        with pytest.raises(AppError):
            await _verify(app, actor, capture)


async def test_postgresql_final_read_uses_one_snapshot(postgres_export_apps, monkeypatch):
    app, writer_app = postgres_export_apps
    async with _client(app) as owner, _client(app) as member:
        _owner, _user, project, _grant, actor, conversation_id = await _world(app, owner, member)
        capture = await _capture(app, actor, project["id"], conversation_id)
        reached, release = asyncio.Event(), asyncio.Event()
        original = export_reads._verify_access

        async def gated(db, frozen):
            fact = await original(db, frozen)
            reached.set()
            await release.wait()
            return fact

        monkeypatch.setattr(export_reads, "_verify_access", gated)
        verifying = asyncio.create_task(_verify(app, actor, capture))
        try:
            await asyncio.wait_for(reached.wait(), 10)
            # 最终快照已经建立，第二个连接提交会话改名；同一事务中的后续批次仍看到旧版本。
            async with writer_app.state.sessionmaker() as db:
                await db.execute(update(AgentConversation).where(AgentConversation.id == conversation_id)
                                 .values(title="快照开始后修改"))
                await db.commit()
            release.set()
            await asyncio.wait_for(verifying, 10)
        finally:
            release.set()
            if not verifying.done():
                verifying.cancel()
            await asyncio.gather(verifying, return_exceptions=True)
        # 新一次最终快照必须看到提交过的修改，拒绝旧捕获。
        with pytest.raises(AppError):
            await _verify(app, actor, capture)
