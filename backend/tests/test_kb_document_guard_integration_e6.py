"""真实编辑者、固定作者模型与文档 claim 的受控物理发送。"""

from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.ai.providers import AIProviderError, create_provider
from app.core.errors import UnauthorizedError
from app.core.security import ACCESS_TOKEN_COOKIE, decode_access_token
from app.models import AIConfig, KbChunk, KbDocument, RetrievalChunk, Task, User
from app.services import ai_dispatch, collaboration_ai_guard, kb_index_service, project_ai_config, retrieval_service
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.task_engine.execution_identity import from_queued_task
from tests.helpers import create_project, register
from tests.test_ai_visibility_dispatch import _server


async def _editor_execution(app, owner, editor, url):
    await app.state.task_engine.stop()
    author = await register(owner, "guard-author")
    actor = await register(editor, "guard-editor")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    invited = await owner.post(f"{base}/collaborators", json={
        "username": "guard-editor", "role": "editor", "allow_owner_ai": True,
    })
    assert invited.status_code == 201, invited.text
    invitation = invited.json()
    accepted = await editor.post(
        f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    member = accepted.json()
    token = decode_access_token(editor.cookies.get(ACCESS_TOKEN_COOKIE), app.state.settings.jwt_secret)
    assert token and token[0] == actor["id"]
    async with app.state.sessionmaker() as db:
        config = AIConfig(user_id=author["id"], name="作者向量", provider="openai_compat", model="embed-test",
                          base_url=f"{url}/v1", api_key_enc=app.state.cipher.encrypt("test-key"),
                          model_type="embedding", is_default=True)
        doc = KbDocument(project_id=project["id"], filename="资料.txt", sha256="a" * 64, text="原始资料")
        db.add_all([config, doc])
        await db.flush()
        selection = await project_ai_config.resolve_shared_embedding(
            db, actor["id"], project["id"], cipher=app.state.cipher, allow_private=True,
        )
        assert selection.mode == "vector" and selection.project_owner_id == author["id"]
        task = Task(user_id=actor["id"], project_id=project["id"], kind="kb.index", status="queued",
                    resource_key=f"kbdoc:{doc.id}", input={
                        "request": {"project_id": project["id"], "mode": "document", "document_id": doc.id},
                        "_task_actor_token_version": token[1],
                        "_collaboration_grant": {"invitation_id": member["invitation_id"],
                                                 "revision": member["revision"]},
                        "_shared_embedding": project_ai_config.serialize_shared_embedding(selection),
                    })
        db.add(task)
        await db.flush()
        queued = await kb_index_service.bind_queued_document_claim(db, task.id, task.statistics_key,
                                                                    queue_claim_key=str(uuid4()))
        key = str(uuid4())
        execution = from_queued_task(task, key)
        await db.execute(update(Task).where(Task.id == task.id).values(status="running", execution_key=key))
        claim = await kb_index_service.activate_document_claim(db, queued, execution_key=key)
        await db.commit()
        stamp = await ai_dispatch.read_visibility_stamp(db, project["id"])
    return actor, member, base, execution, claim, selection, stamp


async def _document_snapshots(app, claim):
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, claim.document_id)
        await kb_index_service.chunk_document(db, doc)
        await db.commit()
        row = await db.scalar(select(RetrievalChunk).where(RetrievalChunk.project_id == claim.project_id))
        return tuple(await retrieval_service.snapshot_embedding_inputs(db, claim.project_id, [row.id]))


@pytest.mark.parametrize("change", ["grant", "token", "document_claim", "task_execution", "source_text"])
async def test_task_guard_fences_document_before_preflight_send_and_write(app, client, make_client, change):
    payload = b'{"object":"list","data":[{"object":"embedding","index":0,"embedding":[1.0,0.0]}]}'
    async with _server(mime="application/json", payload=payload) as (url, _, headers, body, requests):
        headers.set()
        body.set()
        editor = make_client()
        actor, member, base, execution, claim, selection, stamp = await _editor_execution(app, client, editor, url)
        snapshots = await _document_snapshots(app, claim)
        text = snapshots[0].embed_text[:retrieval_service.EMBED_TEXT_CHARS]
        guard = collaboration_ai_guard.for_task(
            execution, selection, stamp, cipher=app.state.cipher, allow_private=True, document_claim=claim,
            embedding_inputs=snapshots,
        )
        bound = await guard.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=5)
        await bound.provider.preflight()
        assert await bound.provider.embed([text], bound.model) == [[1.0, 0.0]]
        assert len(requests) == 1
        with pytest.raises((AIProviderError, VisibilityPolicyError)):
            await bound.provider.embed(["未经快照的其他正文"], bound.model)
        assert len(requests) == 1
        async with app.state.sessionmaker() as db:
            await guard.require_task_write(db)
            await db.rollback()
        if change == "grant":
            changed = await client.patch(f"{base}/collaborators/{actor['id']}", json={
                "expected_revision": member["revision"], "allow_owner_ai": False,
            })
            assert changed.status_code == 200, changed.text
        else:
            async with app.state.sessionmaker() as db:
                if change == "token":
                    user = await db.get(User, actor["id"])
                    user.token_version += 1
                elif change == "document_claim":
                    await db.execute(update(KbDocument).where(KbDocument.id == claim.document_id).values(
                        index_claim_key=str(uuid4()),
                    ))
                elif change == "task_execution":
                    await db.execute(update(Task).where(Task.id == claim.task_id).values(execution_key=str(uuid4())))
                else:
                    chunk = await db.get(KbChunk, snapshots[0].ref_id)
                    chunk.content = "资料已被修改"
                await db.commit()
        with pytest.raises(VisibilityPolicyError):
            await bound.provider.preflight()
        with pytest.raises((AIProviderError, VisibilityPolicyError, UnauthorizedError)):
            await bound.provider.embed([text], bound.model)
        async with app.state.sessionmaker() as db:
            with pytest.raises(VisibilityPolicyError):
                await guard.require_task_write(db)
        assert len(requests) == 1
