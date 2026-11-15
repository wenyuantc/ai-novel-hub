"""PostgreSQL 双连接：编辑者采纳与撤权按同一父锁提交顺序裁决。"""

import asyncio

import httpx
from sqlalchemy import select

from app.models import Chapter, GenerationLog, SceneReviewProposal
from app.services import rich_document_review
from tests.test_collaboration_editor_proposal import _editor_proposal

pytest_plugins = ("tests.test_collaboration_review_read_postgresql",)


async def test_editor_accept_first_blocks_revocation_until_commit(postgres_review_apps, monkeypatch):
    app, _ = postgres_review_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as owner:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as editor:
            project, chapter, editor_user, member, document_url, _source, job, identity, body = await _editor_proposal(
                app, owner, editor,
            )
            original = rich_document_review._read_payload
            entered, release = asyncio.Event(), asyncio.Event()

            async def hold_locked(db, proposal):
                result = await original(db, proposal)
                entered.set()
                await release.wait()
                return result

            monkeypatch.setattr(rich_document_review, "_read_payload", hold_locked)
            accept = asyncio.create_task(editor.post(
                f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept", json=body,
            ))
            revoke = None
            try:
                await asyncio.wait_for(entered.wait(), 10)
                revoke = asyncio.create_task(owner.delete(
                    f"/api/v1/projects/{project['id']}/collaborators/{editor_user['id']}",
                    params={"expected_revision": member["revision"]},
                ))
                done, _ = await asyncio.wait({revoke}, timeout=0.2)
                assert not done
                release.set()
                assert (await asyncio.wait_for(accept, 10)).status_code == 200
                assert (await asyncio.wait_for(revoke, 10)).status_code == 204
            finally:
                release.set()
                for pending in (accept, revoke):
                    if pending is not None and not pending.done():
                        pending.cancel()
                await asyncio.gather(*(pending for pending in (accept, revoke) if pending), return_exceptions=True)
            assert (await owner.get(document_url)).json()["document"]["content"] == "编辑者生成的候选正文"
            async with app.state.sessionmaker() as db:
                assert (await db.get(Chapter, chapter["id"])).status == "done"
                log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == job.task_id))
                assert log.actor_user_id == editor_user["id"]


async def test_revocation_before_editor_lock_prevents_accept(postgres_review_apps, monkeypatch):
    app, _ = postgres_review_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as owner:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as editor:
            project, chapter, editor_user, member, document_url, source, job, identity, body = await _editor_proposal(
                app, owner, editor,
            )
            original = rich_document_review._owned_proposal
            entered, release = asyncio.Event(), asyncio.Event()

            async def hold_initial(db, *args, **kwargs):
                result = await original(db, *args, **kwargs)
                if not kwargs.get("lock", False):
                    entered.set()
                    await release.wait()
                return result

            monkeypatch.setattr(rich_document_review, "_owned_proposal", hold_initial)
            accept = asyncio.create_task(editor.post(
                f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept", json=body,
            ))
            try:
                await asyncio.wait_for(entered.wait(), 10)
                revoked = await owner.delete(f"/api/v1/projects/{project['id']}/collaborators/{editor_user['id']}",
                                             params={"expected_revision": member["revision"]})
                assert revoked.status_code == 204
                release.set()
                assert (await asyncio.wait_for(accept, 10)).status_code == 404
            finally:
                release.set()
                if not accept.done():
                    accept.cancel()
                await asyncio.gather(accept, return_exceptions=True)
            assert (await owner.get(document_url)).json() == source
            async with app.state.sessionmaker() as db:
                assert (await db.get(Chapter, chapter["id"])).content == "作者原稿🙂"
                proposal = await db.scalar(
                    select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"])
                )
                assert proposal.status == "pending"
