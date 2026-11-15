"""本机真实 HTTP 与隔离 PostgreSQL 验证逐章发送的两种提交顺序。"""

import asyncio
import json
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text, update

from app.ai.providers import create_provider
from app.models import Project, ProjectCollaborator, Task
from app.services import ai_dispatch, chapter_claim, collaboration_service, project_access
from app.services.ai_dispatch import VisibilityPolicyError, read_visibility_stamp
from app.services.collaboration_ai_guard import _BoundModelProvider
from tests.test_ai_visibility_dispatch import MESSAGES, _collect, _server, _sse
from tests.test_chapter_claim_postgresql import _batch
from tests.test_chapter_claim_postgresql import postgres_chapter_apps as _postgres_fixture
from tests.test_collaboration_access_postgresql import _setup, _wait_for_blocker

postgres_chapter_apps = _postgres_fixture


async def _ready(app, owner, guest):
    project, editor, base = await _setup(owner, guest)
    maker = app.state.sessionmaker
    async with maker() as db:
        owner_id = await db.scalar(select(Project.user_id).where(Project.id == project["id"]))
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == editor["id"],
        ))
        grant = (member.invitation_id, member.revision)
    identity, execution = await _batch(maker, actor_id=editor["id"], project_id=project["id"], grant=grant)
    async with maker() as db:
        claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
        stamp = await read_visibility_stamp(db, project["id"])
        await db.commit()
    return owner_id, editor, base, identity, execution, claim, stamp


async def _reset_and_reclaim(db, identity, execution, old_claim):
    await project_access.require_project(
        db, identity["actor_id"], identity["project_id"], "ai.use", for_update=True, visibility_write=True,
        grant_identity=identity["grant_identity"],
    )
    await db.scalar(select(Task.id).where(Task.id == identity["task_id"]).with_for_update())
    assert await chapter_claim.reset_generating_chapter(
        db, identity["actor_id"], identity["project_id"], identity["chapter_id"], "draft",
    )
    await db.execute(update(Task).where(Task.id == identity["task_id"]).values(
        checkpoint={"phase": "generate", "current": None, "items": []},
    ))
    new_claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
    assert new_claim.claim_key != old_claim.claim_key
    return new_claim


@pytest.mark.parametrize("method", ["text", "embedding"])
@pytest.mark.parametrize("operation", ["reset", "revoke"])
@pytest.mark.parametrize("send_first", [False, True])
async def test_real_chapter_send_and_mutation_are_serialized(
    postgres_chapter_apps, method, operation, send_first,
):
    owner_app, guest_app = postgres_chapter_apps
    maker = guest_app.state.sessionmaker
    payload = (_sse("openai") if method == "text" else json.dumps({
        "object": "list", "model": "test-model", "data": [{"index": 0, "embedding": [1.0]}],
    }).encode())
    mime = "text/event-stream" if method == "text" else "application/json"
    async with (
        _server(payload=payload, mime=mime) as (url, received, headers, body, requests),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        owner_id, editor, base, identity, execution, claim, stamp = await _ready(owner_app, owner, guest)
        provider = ai_dispatch.chapter_provider_factory(create_provider)("openai_compat", "key", f"{url}/v1/", 15)
        send_pid = []

        async def authorize(db):
            await ai_dispatch.authorize_send_actor(db, claim.actor_id)
            send_pid.append(await db.scalar(text("SELECT pg_backend_pid()")))

        async def verify(db):
            await chapter_claim.require_sending_chapter(db, claim)

        guarded = ai_dispatch.guard_chapter_provider(
            _BoundModelProvider(provider, "test-model", method), stamp, maker,
            chapter=claim, authorize=authorize, verify=verify,
        )

        async def send():
            if method == "text":
                return await _collect(guarded.generate_stream(MESSAGES, "test-model"))
            return await guarded.embed(["章节向量"], "test-model")

        async def mutate():
            if operation == "revoke":
                response = await owner.delete(
                    f"{base}/collaborators/{editor['id']}", params={"expected_revision": 2},
                )
                assert response.status_code == 204, response.text
            else:
                async with maker() as db:
                    await _reset_and_reclaim(db, identity, execution, claim)
                    await db.commit()

        sending = change = None
        try:
            if send_first:
                sending = asyncio.create_task(send())
                await asyncio.wait_for(received.wait(), 10)
                assert len(send_pid) == 1 and len(requests) == 1
                change = asyncio.create_task(mutate())
                await asyncio.wait_for(_wait_for_blocker(maker, send_pid[0]), 10)
                assert not change.done()
                headers.set()
                await asyncio.wait_for(change, 10)
                assert not sending.done()  # body 尚未抵达，写端必须已经完成。
                body.set()
                await asyncio.wait_for(sending, 10)
                with pytest.raises(VisibilityPolicyError):
                    await send()
                assert len(requests) == 1
            else:
                async with maker() as holder:
                    if operation == "reset":
                        await _reset_and_reclaim(holder, identity, execution, claim)
                    else:
                        await collaboration_service._lock_parent(
                            holder, owner_id, "project", claim.project_id,
                            target_id=editor["id"], owner_only=True,
                        )
                        await holder.execute(update(ProjectCollaborator).where(
                            ProjectCollaborator.project_id == claim.project_id,
                            ProjectCollaborator.user_id == editor["id"],
                        ).values(status="revoked", revision=ProjectCollaborator.revision + 1))
                    pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                    sending = asyncio.create_task(send())
                    await asyncio.wait_for(_wait_for_blocker(maker, pid), 10)
                    assert not received.is_set() and requests == []
                    await holder.commit()
                with pytest.raises(VisibilityPolicyError):
                    await asyncio.wait_for(sending, 10)
                assert not received.is_set() and requests == []
        finally:
            headers.set()
            body.set()
            for task in (sending, change):
                if task is not None and not task.done():
                    task.cancel()
            for task in (sending, change):
                if task is not None:
                    with suppress(BaseException):
                        await task
