"""可阅读导出的本人聊天及最终权限屏障。"""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text, update

from app.core.errors import AppError, NotFoundError, UnauthorizedError
from app.models import AgentConversation, AgentMessage, Project, ProjectCollaborator, Task, User
from app.services.agent import export_reads
from app.services.agent.export_reads import capture_document_access, verify_document_access
from app.services.authorization_service import AuthenticatedActor
from tests.helpers import create_project, register


async def _fixture(app, client):
    owner = await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        user = await db.get(User, owner["id"])
        actor = AuthenticatedActor(user.id, user.token_version)
        first = AgentConversation(user_id=user.id, project_id=project["id"], title="首次会话")
        empty = AgentConversation(user_id=user.id, project_id=project["id"], title="空会话")
        other = AgentConversation(user_id=user.id, project_id=project["id"], title="未选会话")
        db.add_all([first, empty, other])
        await db.flush()
        messages = [
            AgentMessage(conversation_id=first.id, role="user", content="用户文本"),
            AgentMessage(conversation_id=first.id, role="tool", content="TOOL-SECRET"),
            AgentMessage(conversation_id=first.id, role="assistant", content=json.dumps({
                "story": "可以阅读", "api_key": "MODEL-SECRET", "task_id": 987654,
            }, ensure_ascii=False)),
            AgentMessage(conversation_id=other.id, role="assistant", content="UNSELECTED-SECRET"),
        ]
        db.add_all(messages)
        await db.commit()
        return project["id"], actor, first.id, empty.id, other.id, messages[0].id


async def _capture(app, actor, project_id, ids):
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        capture = await capture_document_access(db, actor.id, project_id, ids)
        await db.rollback()
        await verify_document_access(db, capture)
        return capture


async def test_selected_chat_filters_internal_text_and_detects_changes(app, client):
    project_id, actor, first, empty, _other, message_id = await _fixture(app, client)
    capture = await _capture(app, actor, project_id, [first, empty])
    data = json.dumps(capture.conversations, ensure_ascii=False)
    assert "用户文本" in data and "可以阅读" in data and "空会话" in data
    assert "TOOL-SECRET" not in data and "MODEL-SECRET" not in data and "UNSELECTED-SECRET" not in data
    assert len(next(c for c in capture.conversations if c["id"] == first)["messages"]) == 2

    async with app.state.sessionmaker() as db:
        await db.execute(update(AgentMessage).where(AgentMessage.id == message_id).values(content="后续篡改"))
        await db.commit()
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        with pytest.raises(AppError):
            await verify_document_access(db, capture)


async def test_empty_selected_conversation_is_verified(app, client):
    project_id, actor, _first, empty, _other, _message_id = await _fixture(app, client)
    capture = await _capture(app, actor, project_id, [empty])
    async with app.state.sessionmaker() as db:
        row = await db.get(AgentConversation, empty)
        row.title = "更名"
        await db.commit()
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        with pytest.raises(AppError):
            await verify_document_access(db, capture)


@pytest.mark.parametrize("change", ["token", "recycle_restore"])
async def test_no_chat_still_checks_original_login_and_project_lifecycle(app, client, change):
    project_id, actor, *_ = await _fixture(app, client)
    capture = await _capture(app, actor, project_id, [])
    async with app.state.sessionmaker() as db:
        if change == "token":
            user = await db.get(User, actor.id)
            user.token_version += 1
        else:
            project = await db.get(Project, project_id)
            project.last_deleted_at = datetime.now(UTC)
        await db.commit()
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        with pytest.raises(UnauthorizedError if change == "token" else AppError):
            await verify_document_access(db, capture)


async def test_viewer_old_conversation_and_reinvite_invalidates_capture(app, client, make_client):
    owner = await register(client)
    project = await create_project(client)
    member_client = make_client()
    member = await register(member_client, "export-viewer")
    async with app.state.sessionmaker() as db:
        user = await db.get(User, member["id"])
        actor = AuthenticatedActor(user.id, user.token_version)
        grant = ProjectCollaborator(project_id=project["id"], user_id=user.id, role="viewer", status="accepted",
                                    invited_by_user_id=owner["id"])
        db.add(grant)
        # 老轮次会话没有当前邀请标记，允许只读。
        conversation = AgentConversation(user_id=user.id, project_id=project["id"], title="旧会话")
        db.add(conversation)
        await db.flush()
        db.add(AgentMessage(conversation_id=conversation.id, role="assistant", content="旧会话正文"))
        await db.commit()
        cid = conversation.id
    capture = await _capture(app, actor, project["id"], [cid])
    assert capture.conversations[0]["messages"][0]["content"] == "旧会话正文"
    async with app.state.sessionmaker() as db:
        grant = await db.get(ProjectCollaborator, grant.id)
        grant.revision += 2
        await db.commit()
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        with pytest.raises(AppError):
            await verify_document_access(db, capture)


async def test_non_owned_chat_is_not_selectable(app, client, make_client):
    project_id, actor, first, *_ = await _fixture(app, client)
    stranger = make_client()
    user = await register(stranger, "private-stranger")
    async with app.state.sessionmaker() as db:
        account = await db.get(User, user["id"])
        stranger_actor = AuthenticatedActor(account.id, account.token_version)
        db.info["authenticated_actor"] = stranger_actor
        with pytest.raises(NotFoundError):
            await capture_document_access(db, stranger_actor.id, project_id, [first])


@pytest.mark.parametrize("wal", [False, True])
async def test_sqlite_large_message_proofs_are_bounded(app, client, wal):
    project_id, actor, first, *_ = await _fixture(app, client)
    async with app.state.sessionmaker() as db:
        if wal:
            assert (await db.scalar(text("PRAGMA journal_mode=WAL"))).lower() == "wal"
        db.add_all([AgentMessage(conversation_id=first, role="user", content=f"第 {index} 条")
                    for index in range(451)])
        await db.commit()
    capture = await _capture(app, actor, project_id, [first])
    assert len(capture.conversations[0]["messages"]) == 453
    assert capture.conversations[0]["messages"][-1]["content"] == "第 450 条"


async def test_many_empty_conversations_have_individual_proofs(app, client):
    project_id, actor, *_ = await _fixture(app, client)
    async with app.state.sessionmaker() as db:
        rows = [AgentConversation(project_id=project_id, user_id=actor.id, title=f"空会话 {i}") for i in range(405)]
        db.add_all(rows)
        await db.commit()
        ids = [row.id for row in rows]
    capture = await _capture(app, actor, project_id, ids)
    assert len(capture.conversations) == len(ids)
    async with app.state.sessionmaker() as db:
        await db.execute(update(AgentConversation).where(AgentConversation.id == ids[-1]).values(title="已改变"))
        await db.commit()
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        with pytest.raises(AppError):
            await verify_document_access(db, capture)


async def test_sqlite_wal_final_reads_share_the_same_snapshot(app, client, monkeypatch):
    project_id, actor, first, *_ = await _fixture(app, client)
    async with app.state.sessionmaker() as db:
        assert (await db.scalar(text("PRAGMA journal_mode=WAL"))).lower() == "wal"
    capture = await _capture(app, actor, project_id, [first])
    reached, release = asyncio.Event(), asyncio.Event()
    original = export_reads._verify_access

    async def gated(db, frozen):
        fact = await original(db, frozen)
        reached.set()
        await release.wait()
        return fact

    async def run_verify():
        async with app.state.sessionmaker() as db:
            db.info["authenticated_actor"] = actor
            await verify_document_access(db, capture)

    monkeypatch.setattr(export_reads, "_verify_access", gated)
    verifying = asyncio.create_task(run_verify())
    try:
        await asyncio.wait_for(reached.wait(), 5)
        async with app.state.sessionmaker() as db:
            await db.execute(update(AgentConversation).where(AgentConversation.id == first).values(title="新标题"))
            await db.commit()
        release.set()
        await asyncio.wait_for(verifying, 5)
    finally:
        release.set()
        if not verifying.done():
            verifying.cancel()
        await asyncio.gather(verifying, return_exceptions=True)
    with pytest.raises(AppError):
        await run_verify()


async def test_private_reference_visibility_change_discards_document(app, client):
    project_id, actor, first, _empty, _other, message_id = await _fixture(app, client)
    async with app.state.sessionmaker() as db:
        task = Task(user_id=actor.id, project_id=project_id, kind="agent.turn", status="succeeded")
        db.add(task)
        await db.flush()
        message = await db.get(AgentMessage, message_id)
        message.content = json.dumps({"task_id": task.id, "story": "文字保留"}, ensure_ascii=False)
        task_id = task.id
        await db.commit()
    capture = await _capture(app, actor, project_id, [first])
    assert json.loads(capture.conversations[0]["messages"][0]["content"])["task_id"] == task_id
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        await db.delete(task)
        await db.commit()
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        with pytest.raises(AppError):
            await verify_document_access(db, capture)


async def test_cancelled_verification_releases_sqlite_read_transaction(app, client, monkeypatch):
    project_id, actor, first, *_ = await _fixture(app, client)
    capture = await _capture(app, actor, project_id, [first])
    reached = asyncio.Event()

    async def blocked(_db, _capture, _fact):
        reached.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(export_reads, "_verify_conversations", blocked)

    async def run_verify():
        async with app.state.sessionmaker() as db:
            db.info["authenticated_actor"] = actor
            await verify_document_access(db, capture)

    verifying = asyncio.create_task(run_verify())
    await asyncio.wait_for(reached.wait(), 5)
    verifying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await verifying

    # 此写提交需要读者释放 SQLite 事务；遗留读锁会在超时前无法完成。
    async def write_after_cancel():
        async with app.state.sessionmaker() as db:
            await db.execute(update(AgentConversation).where(AgentConversation.id == first).values(title="可写"))
            await db.commit()

    await asyncio.wait_for(write_after_cancel(), 5)
