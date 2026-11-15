"""文档 claim 只有真实发送租约或持锁写事务才能验证。"""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.ai.providers import AIProviderError, OpenAICompatProvider
from app.core.errors import ConflictError, UnauthorizedError
from app.core.security import ACCESS_TOKEN_COOKIE, decode_access_token
from app.models import KbDocument, Task, User
from app.services import ai_dispatch, authorization_service
from app.services import kb_index_service as indexes
from app.services.ai_dispatch import VisibilityPolicyError
from tests.test_ai_visibility_dispatch import _server
from tests.test_kb_index_claims import _seed


async def _running(app, client):
    uid, pid, did, tid, key = await _seed(app, client)
    decoded = decode_access_token(client.cookies.get(ACCESS_TOKEN_COOKIE), app.state.settings.jwt_secret)
    assert decoded and decoded[0] == uid
    actor = authorization_service.AuthenticatedActor(uid, decoded[1])
    async with app.state.sessionmaker() as db:
        queued = await indexes.bind_queued_document_claim(db, tid, key, queue_claim_key=str(uuid4()))
        execution = str(uuid4())
        await db.execute(update(Task).where(Task.id == tid).values(status="running", execution_key=execution))
        claim = await indexes.activate_document_claim(db, queued, execution_key=execution)
        await db.commit()
        stamp = await ai_dispatch.read_visibility_stamp(db, pid)
    return actor, claim, stamp


@pytest.mark.parametrize("preflight", [False, True])
async def test_document_send_check_cannot_be_forged_without_physical_lease(app, client, preflight):
    actor, claim, _ = await _running(app, client)
    async with app.state.sessionmaker() as db:
        with pytest.raises(VisibilityPolicyError):
            await indexes.require_sending_document_claim(db, claim, actor=actor, grant_identity=None,
                                                         preflight=preflight)


@pytest.mark.parametrize("change", ["task_key", "execution", "queue", "document", "actor"])
async def test_document_write_check_fences_original_claim_and_rolls_back(app, client, change):
    actor, claim, _ = await _running(app, client)
    alterations = {
        "task_key": {"task_key": str(uuid4())}, "execution": {"claim_key": str(uuid4())},
        "queue": {"queued_at": claim.queued_at + timedelta(seconds=1)},
        "document": {"document_id": claim.document_id + 1}, "actor": {"actor_id": claim.actor_id + 1},
    }
    async with app.state.sessionmaker() as db:
        await indexes.require_writing_document_claim(db, claim, actor=actor, grant_identity=None)
        await db.rollback()
        doc = await db.get(KbDocument, claim.document_id)
        doc.filename = "不能提交"
        with pytest.raises(ConflictError):
            await indexes.require_writing_document_claim(db, replace(claim, **alterations[change]),
                                                         actor=actor, grant_identity=None)
        await db.commit()
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, claim.document_id)
        assert doc.filename == "文档" and doc.index_claim_key == claim.claim_key


async def test_document_claim_checks_real_preflight_and_http_send(app, client):
    actor, claim, stamp = await _running(app, client)
    checked = []

    async def authorize(db):
        await ai_dispatch.authorize_send_actor(db, actor)

    async def verify(db):
        await indexes.require_sending_document_claim(db, claim, actor=actor, grant_identity=None, preflight=False)
        checked.append("send")

    async def preflight(db):
        await indexes.require_sending_document_claim(db, claim, actor=actor, grant_identity=None, preflight=True)
        checked.append("preflight")

    payload = b'{"object":"list","data":[{"object":"embedding","index":0,"embedding":[1.0,0.0]}]}'
    async with _server(mime="application/json", payload=payload) as (url, _, headers, body, requests):
        headers.set()
        body.set()
        provider = ai_dispatch.guard_provider(
            OpenAICompatProvider("test-key", f"{url}/v1", 5), stamp, app.state.sessionmaker,
            authorize=authorize, verify=verify, verify_preflight=preflight,
        )
        await provider.preflight()
        assert await provider.embed(["合法资料"], "test-model") == [[1.0, 0.0]]
        assert checked == ["preflight", "send"] and len(requests) == 1
        async with app.state.sessionmaker() as db:
            user = await db.get(User, actor.id)
            user.token_version += 1
            await db.commit()
        with pytest.raises((AIProviderError, VisibilityPolicyError, UnauthorizedError)):
            await provider.embed(["不得再次发送"], "test-model")
        assert len(requests) == 1
