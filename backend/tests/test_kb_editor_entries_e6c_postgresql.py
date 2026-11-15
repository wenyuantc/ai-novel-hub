"""E6-c：授权编辑者 HTTP 向量入口与撤权在真实 PostgreSQL 跨实例 / 跨连接的两种提交顺序。

- 撤权先提交（持锁）：编辑者上传在锁上阻塞，放行后被拒，零文档零任务零调用。
- 上传受理先提交（持锁）：撤权在锁上阻塞（pg_blocking_pids），放行后任务已落库归编辑者，
  随后领取按数据库最新授权失败关闭并释放文档占用，零调用。
- 人工记忆派生受理先提交、撤权后：领取失败关闭，零调用。
"""

import asyncio
from contextlib import suppress

import httpx
from sqlalchemy import func, select, text

from app.core.errors import AppError
from app.models import KbDocument, Task
from app.services import collaboration_service, project_ai_config
from tests.helpers import create_ai_config, create_project, register
from tests.test_collaboration_access_postgresql import _assert_project_row_locked, _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


def _clients(owner_app, guest_app):
    return (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver"),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver"),
    )


async def _setup(owner, guest):
    await register(owner, "pg-e6c-owner")
    guest_user = await register(guest, "pg-e6c-guest")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    invited = await owner.post(f"{base}/collaborators", json={
        "username": "pg-e6c-guest", "role": "editor", "allow_owner_ai": True,
    })
    assert invited.status_code == 201, invited.text
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invited.json()['invitation_id']}/accept",
        json={"expected_revision": invited.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    await create_ai_config(owner, name="作者向量", model_type="embedding", model="owner-emb")
    await create_ai_config(guest, name="个人向量", model_type="embedding", model="guest-emb")
    return project["id"], guest_user["id"], base, accepted.json()


async def _cancel(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, AppError):
                await task


async def test_revoke_commits_before_editor_vector_upload(postgres_collaboration_apps, monkeypatch, fake_llm):
    owner_app, guest_app = postgres_collaboration_apps
    owner_client, guest_client = _clients(owner_app, guest_app)
    async with owner_client as owner, guest_client as guest:
        project_id, guest_id, base, member = await _setup(owner, guest)
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = collaboration_service.add_success

        async def gated(db, snapshot):
            event = await original(db, snapshot)
            if snapshot.action == "revoke":
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return event

        monkeypatch.setattr(collaboration_service, "add_success", gated)
        revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{guest_id}", params={
            "expected_revision": member["revision"],
        }))
        upload = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            await _assert_project_row_locked(guest_app.state.sessionmaker, project_id)
            upload = asyncio.create_task(guest.post(f"{base}/kb/documents", files={
                "file": ("撤权后.txt", "撤权后的资料".encode()),
            }))
            await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, holder["pid"]), 8)
            release.set()
            assert (await asyncio.wait_for(revoke, 8)).status_code == 204
            response = await asyncio.wait_for(upload, 8)
            assert response.status_code in (403, 404), response.text
            async with owner_app.state.sessionmaker() as db:
                assert await db.scalar(select(func.count(Task.id))) == 0
                assert await db.scalar(select(func.count(KbDocument.id))) == 0
            assert not fake_llm.embed_calls
        finally:
            release.set()
            await _cancel(revoke, upload)


async def test_editor_upload_commits_before_revoke_then_claim_fails_closed(
    postgres_collaboration_apps, monkeypatch, fake_llm,
):
    owner_app, guest_app = postgres_collaboration_apps
    owner_client, guest_client = _clients(owner_app, guest_app)
    async with owner_client as owner, guest_client as guest:
        project_id, guest_id, base, member = await _setup(owner, guest)
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = project_ai_config.resolve_shared_embedding

        async def gated(db, *args, **kwargs):
            selection = await original(db, *args, **kwargs)
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
            return selection

        monkeypatch.setattr(project_ai_config, "resolve_shared_embedding", gated)
        upload = asyncio.create_task(guest.post(f"{base}/kb/documents", files={
            "file": ("先受理.txt", "先受理的资料".encode()),
        }))
        revoke = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            await _assert_project_row_locked(owner_app.state.sessionmaker, project_id)
            revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{guest_id}", params={
                "expected_revision": member["revision"],
            }))
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 8)
            release.set()
            response = await asyncio.wait_for(upload, 8)
            # 受理已提交；撤权在返回前的任务投影复核之前提交时 404 也是合法赢家，以库内事实为准。
            assert response.status_code in (202, 404), response.text
            assert (await asyncio.wait_for(revoke, 8)).status_code == 204
        finally:
            release.set()
            await _cancel(upload, revoke)
        monkeypatch.setattr(project_ai_config, "resolve_shared_embedding", original)
        async with owner_app.state.sessionmaker() as db:
            row = await db.scalar(select(Task).where(Task.kind == "kb.index"))
            assert row is not None and row.user_id == guest_id and row.status == "queued"
            assert row.input["_collaboration_grant"]["invitation_id"] == member["invitation_id"]
            doc = await db.scalar(select(KbDocument).where(KbDocument.project_id == project_id))
            assert doc.index_task_key == row.statistics_key
            task_id, task_key, doc_id = row.id, row.statistics_key, doc.id
        await guest_app.state.task_engine._claim_and_run(task_id, task_key)
        async with owner_app.state.sessionmaker() as db:
            row = await db.get(Task, task_id)
            assert row.status == "failed"
            doc = await db.get(KbDocument, doc_id)
            assert doc.index_task_key is None and doc.index_claim_key is None
        assert not fake_llm.embed_calls


async def test_editor_memory_admission_commits_before_revoke_then_claim_fails_closed(
    postgres_collaboration_apps, monkeypatch, fake_llm,
):
    owner_app, guest_app = postgres_collaboration_apps
    owner_client, guest_client = _clients(owner_app, guest_app)
    async with owner_client as owner, guest_client as guest:
        project_id, guest_id, base, member = await _setup(owner, guest)
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = project_ai_config.resolve_shared_embedding

        async def gated(db, *args, **kwargs):
            selection = await original(db, *args, **kwargs)
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
            return selection

        monkeypatch.setattr(project_ai_config, "resolve_shared_embedding", gated)
        write = asyncio.create_task(guest.post(f"{base}/memories", json={"chapter_no": 1, "content": "萧掌门闭关"}))
        revoke = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            await _assert_project_row_locked(owner_app.state.sessionmaker, project_id)
            revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{guest_id}", params={
                "expected_revision": member["revision"],
            }))
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 8)
            release.set()
            # 记忆本身已提交；派生受理已提交，返回前的最终写屏障看到撤权 → 403/404 合法。
            response = await asyncio.wait_for(write, 8)
            assert response.status_code in (201, 403, 404), response.text
            assert (await asyncio.wait_for(revoke, 8)).status_code == 204
        finally:
            release.set()
            await _cancel(write, revoke)
        monkeypatch.setattr(project_ai_config, "resolve_shared_embedding", original)
        async with owner_app.state.sessionmaker() as db:
            row = await db.scalar(select(Task).where(Task.kind == "kb.index"))
            assert row is not None and row.user_id == guest_id and len(row.input["_manual_memory_ref_ids"]) == 1
            task_id, task_key = row.id, row.statistics_key
        await guest_app.state.task_engine._claim_and_run(task_id, task_key)
        async with owner_app.state.sessionmaker() as db:
            assert (await db.get(Task, task_id)).status == "failed"
        assert not fake_llm.embed_calls


async def test_concurrent_vector_entries_and_revision_change_do_not_deadlock(postgres_collaboration_apps, fake_llm):
    """锁序一致性：编辑者上传/重建/记忆、作者上传/记忆与作者修改成员（发送写门禁 → 作品父行）在两实例并发，
    同时引擎领取与受控发送（User 读锁 → 共享门禁 → 作品）也在进行；任一死锁都会表现为 500 或超时。"""
    owner_app, guest_app = postgres_collaboration_apps
    owner_client, guest_client = _clients(owner_app, guest_app)
    async with owner_client as owner, guest_client as guest:
        project_id, guest_id, base, member = await _setup(owner, guest)
        # 单进程任务引擎（部署前提）：在同一时间窗口内领取与受控发送。
        await owner_app.state.task_engine.start()

        def upload(client, name):
            return client.post(f"{base}/kb/documents", files={"file": (f"{name}.txt", f"{name}的资料正文".encode())})

        requests = [
            upload(guest, "编辑者一"), upload(owner, "作者一"), upload(guest, "编辑者二"), upload(owner, "作者二"),
            guest.post(f"{base}/memories", json={"chapter_no": 1, "content": "编辑者记忆"}),
            owner.post(f"{base}/memories", json={"chapter_no": 2, "content": "作者记忆"}),
            guest.post(f"{base}/kb/rebuild"),
            # 成员修改（同值或递增 revision 均可）取发送写门禁与作品父行，与上面的入口竞争同一组锁。
            owner.patch(f"{base}/collaborators/{guest_id}", json={
                "expected_revision": member["revision"], "allow_owner_ai": True,
            }),
        ]
        responses = await asyncio.wait_for(asyncio.gather(*requests), 60)
        for response in responses:
            assert response.status_code < 500, response.text
            # 成员修改可能使编辑者的后续请求看到新轮次（403/404/409 都是合法赢家），但不能是死锁。
            assert response.status_code in (200, 201, 202, 403, 404, 409), response.text
        owner_app.state.task_engine.notify()
        deadline = asyncio.get_running_loop().time() + 60
        while True:
            async with owner_app.state.sessionmaker() as db:
                open_count = await db.scalar(select(func.count(Task.id)).where(
                    Task.kind == "kb.index", Task.status.in_(("queued", "running")),
                ))
            if not open_count:
                break
            assert asyncio.get_running_loop().time() < deadline, "索引任务未在期限内结束"
            owner_app.state.task_engine.notify()
            await asyncio.sleep(0.1)
        async with owner_app.state.sessionmaker() as db:
            rows = list(await db.scalars(select(Task).where(Task.kind == "kb.index")))
            assert rows and all(row.status in ("succeeded", "failed", "cancelled") for row in rows)
        # 受控发送确实与上述入口并发发生；只调用作者模型，编辑者本人配置从不参与共享索引。
        assert fake_llm.embed_calls
        assert {call["model"] for call in fake_llm.embed_calls} == {"owner-emb"}
