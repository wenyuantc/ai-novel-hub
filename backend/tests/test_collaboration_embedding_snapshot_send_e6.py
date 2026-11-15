"""发送来源快照必须在真实租约内重验，作者旧空间只能由原作者任务写入。"""

import numpy as np
import pytest
from sqlalchemy import select

from app.ai.providers import AIProviderError, OpenAICompatProvider
from app.core.errors import BadRequestError, ConflictError, UnauthorizedError
from app.models import AIConfig, KbChunk, RetrievalChunk
from app.services import ai_dispatch, authorization_service, project_ai_config, retrieval_service
from app.services.ai_dispatch import VisibilityPolicyError
from tests.test_ai_visibility_dispatch import _server
from tests.test_kb_document_guard_integration_e6 import _document_snapshots, _editor_execution


async def test_embedding_snapshot_rejects_fake_lease_and_editor_legacy_write(app, client, make_client):
    async with _server(mime="application/json", payload=b'{}') as (url, _, headers, body, requests):
        headers.set()
        body.set()
        editor = make_client()
        _, _, _, execution, claim, selection, _ = await _editor_execution(app, client, editor, url)
        actor = authorization_service.AuthenticatedActor(execution.actor_id, execution.actor_token_version)
        snapshots = await _document_snapshots(app, claim)
        async with app.state.sessionmaker() as db:
            with pytest.raises(VisibilityPolicyError):
                await retrieval_service.require_sending_embedding_snapshots(
                    db, snapshots, actor=actor, execution=execution, selection=selection,
                    cipher=app.state.cipher, allow_private=True, preflight=False,
                )
        async with app.state.sessionmaker() as db:
            config = await retrieval_service.load_bound_shared_embedding_config(
                db, selection, cipher=app.state.cipher, allow_private=True,
            )
            with pytest.raises((ConflictError, VisibilityPolicyError)):
                await retrieval_service.store_owner_legacy_embedding_snapshots(
                    db, snapshots, [np.array([1.0, 0.0])], config, execution=execution,
                )
            await db.rollback()
        async with app.state.sessionmaker() as db:
            row = await db.get(RetrievalChunk, snapshots[0].id)
            assert row.embedding is None and row.embedding_key is None
        assert requests == []


async def test_changed_embedding_source_never_leaves_real_http_transport(app, client, make_client):
    async with _server(mime="application/json", payload=b'{}') as (url, _, headers, body, requests):
        headers.set()
        body.set()
        editor = make_client()
        _, _, _, execution, claim, selection, stamp = await _editor_execution(app, client, editor, url)
        actor = authorization_service.AuthenticatedActor(execution.actor_id, execution.actor_token_version)
        snapshots = await _document_snapshots(app, claim)

        async def authorize(db):
            await ai_dispatch.authorize_send_actor(db, actor)

        async def verify(db):
            await retrieval_service.require_sending_embedding_snapshots(
                db, snapshots, actor=actor, execution=execution, selection=selection,
                cipher=app.state.cipher, allow_private=True, preflight=False,
            )

        provider = ai_dispatch.guard_provider(
            OpenAICompatProvider("test-key", f"{url}/v1", 5), stamp, app.state.sessionmaker,
            authorize=authorize, verify=verify,
        )
        async with app.state.sessionmaker() as db:
            chunk = await db.get(KbChunk, snapshots[0].ref_id)
            chunk.content = "已变更正文"
            await db.commit()
        with pytest.raises((AIProviderError, VisibilityPolicyError, UnauthorizedError)) as error:
            await provider.embed([snapshots[0].embed_text], "embed-test")
        chain, pending = [], [error.value]
        while pending:
            item = pending.pop()
            if any(item is old for old in chain):
                continue
            chain.append(item)
            pending.extend(value for value in (item.__cause__, item.__context__) if value is not None)
        assert any(isinstance(item, retrieval_service.EmbeddingPolicyError) for item in chain)
        assert requests == []


async def test_bound_selection_rejects_config_integer_id_reuse(app, client, make_client):
    """E6-f：删除 shared:v2 空间键断言（原语已删除）；保留「同整数 ID 重建配置不能冒充原绑定」的价值。"""
    async with _server(mime="application/json", payload=b'{}') as (url, _, headers, body, _):
        headers.set()
        body.set()
        editor = make_client()
        _, _, _, execution, _, selection, _ = await _editor_execution(app, client, editor, url)
        async with app.state.sessionmaker() as db:
            old = await retrieval_service.load_bound_shared_embedding_config(
                db, selection, cipher=app.state.cipher, allow_private=True,
            )
            original = await db.scalar(select(AIConfig).where(AIConfig.id == old.config_id))
            values = {key: getattr(original, key) for key in (
                "id", "user_id", "name", "provider", "model", "base_url", "api_key_enc", "model_type", "is_default",
            )}
            await db.delete(original)
            await db.flush()
            replacement = AIConfig(**values)
            db.add(replacement)
            await db.commit()
            assert replacement.instance_key != selection.model.config_instance_key
        async with app.state.sessionmaker() as db:
            selected = await project_ai_config.resolve_shared_embedding(
                db, execution.actor_id, execution.project_id, cipher=app.state.cipher, allow_private=True,
            )
            new = await retrieval_service.load_bound_shared_embedding_config(
                db, selected, cipher=app.state.cipher, allow_private=True,
            )
            assert new.config_id == old.config_id and new.instance_key != old.instance_key
            assert retrieval_service._bound_config_identity(new) != retrieval_service._bound_config_identity(old)
            with pytest.raises((BadRequestError, VisibilityPolicyError)):
                await retrieval_service.load_bound_shared_embedding_config(
                    db, selection, cipher=app.state.cipher, allow_private=True,
                )

