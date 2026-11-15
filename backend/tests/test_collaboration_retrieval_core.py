"""作品级检索来源、向量空间与快照写入的内部安全原语。

E6-f（裁决 14）：``shared:v2`` 空间键与其公开写入原语（``shared_embedding_model_key`` /
``store_embedding_snapshot(s)``）已删除。原经这些原语覆盖的 CAS 内核 ``_store_embedding_snapshots``
仍被作者旧键写入入口（``store_index_legacy_embedding_snapshots`` / ``store_owner_legacy_embedding_snapshots``）
共用，相关用例改为直接调用内核并断言作者旧键 ``provider:model:dim``；只测 ``shared:v2`` 键格式与匹配的用例删除。
"""

import asyncio
import json
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy import delete, select, update

from app.ai.providers import AIProviderError, OpenAICompatProvider
from app.ai.providers.base import RequestPolicyError
from app.core.errors import ConflictError, ForbiddenError
from app.models import AIConfig, KbChunk, KbDocument, Project, ProjectCollaborator, RetrievalChunk, StoryMemory, User
from app.services import project_ai_config
from app.services import retrieval_service as rs
from app.services.ai_dispatch import VisibilityPolicyError, read_visibility_stamp
from app.services.kb_parser import retrieval_text
from tests.helpers import FakeLLM
from tests.test_ai_visibility_dispatch import _server

OWNER_SPACE = "openai_compat:同名:model"


async def _store(db, snapshots, vectors, config):
    return await rs._store_embedding_snapshots(db, list(snapshots), list(vectors), config)


@pytest.fixture
async def world(app):
    async with app.state.sessionmaker() as db:
        owners = [
            User(username="retrieval-owner-1", password_hash="x"),
            User(username="retrieval-owner-2", password_hash="x"),
        ]
        db.add_all(owners)
        await db.flush()
        projects = [Project(user_id=user.id, title=f"作品 {index}") for index, user in enumerate(owners)]
        db.add_all(projects)
        await db.flush()
        docs = [KbDocument(project_id=p.id, filename=f"{n}.txt", sha256=str(n) * 64) for n, p in enumerate(projects)]
        db.add_all(docs)
        await db.flush()
        chunks = [KbChunk(document_id=d.id, seq=1, heading_path=f"标题 {n}", content=f"正文 {n}")
                  for n, d in enumerate(docs)]
        memory = StoryMemory(
            project_id=projects[0].id, chapter_no=1, kind="manual", content="真实记忆", characters=["角色"],
        )
        db.add_all([*chunks, memory])
        await db.flush()
        rows = [
            RetrievalChunk(project_id=projects[0].id, source="kb", ref_id=chunks[0].id,
                           text=retrieval_text(chunks[0].heading_path, chunks[0].content)),
            RetrievalChunk(project_id=projects[0].id, source="memory", ref_id=memory.id, text="真实记忆\n角色"),
        ]
        configs = [AIConfig(user_id=user.id, name="秘密配置", provider="openai_compat", model="同名:model",
                            base_url="https://embed.example.test/v1", model_type="embedding", is_default=True,
                            api_key_enc=app.state.cipher.encrypt("secret-key")) for user in owners]
        db.add_all([*rows, *configs])
        await db.commit()
        return {"project": projects[0].id, "foreign_project": projects[1].id,
                "owner": owners[0].id, "foreign_owner": owners[1].id,
                "kb": rows[0].id, "memory": rows[1].id, "foreign_chunk": chunks[1].id,
                "kb_ref": chunks[0].id, "memory_ref": memory.id,
                "config": configs[0].id, "foreign_config": configs[1].id}


async def test_snapshot_checks_sources_and_never_exposes_memory_names(app, world):
    async with app.state.sessionmaker() as db:
        snapshots = await rs.snapshot_embedding_inputs(db, world["project"], [world["kb"], world["memory"]])
    assert [item.source for item in snapshots] == ["kb", "memory"]
    assert snapshots[0].embed_text == "标题 0\n正文 0"
    assert snapshots[1].embed_text == "真实记忆"
    assert "真实记忆" not in repr(snapshots[1]) and "角色" not in repr(snapshots[1])


async def test_snapshot_rejects_cross_project_and_forged_content_without_leaks(app, world):
    async with app.state.sessionmaker() as db:
        await db.execute(update(RetrievalChunk).where(RetrievalChunk.id == world["kb"]).values(
            ref_id=world["foreign_chunk"], text="别的书的秘密",
        ))
        await db.commit()
        with pytest.raises(ConflictError) as caught:
            await rs.snapshot_embedding_inputs(db, world["project"], [world["kb"]])
        assert "别的书的秘密" not in str(caught.value)
        await db.execute(update(RetrievalChunk).where(RetrievalChunk.id == world["kb"]).values(
            ref_id=world["kb_ref"], text="伪造的检索正文",
        ))
        await db.commit()
        with pytest.raises(ConflictError):
            await rs.snapshot_embedding_inputs(db, world["project"], [world["kb"]])


async def test_embedding_compare_and_swap_rejects_changed_source_text_or_vector(app, world):
    vector = np.array([0.6, 0.8], dtype=np.float32)
    async with app.state.sessionmaker() as db:
        selected = await project_ai_config.resolve_shared_embedding(
            db, world["owner"], world["project"], cipher=app.state.cipher,
        )
        config = await rs.load_bound_shared_embedding_config(db, selected, cipher=app.state.cipher, allow_private=False)
        key = rs.make_embedding_key(OWNER_SPACE, 2)
        snapshot = (await rs.snapshot_embedding_inputs(db, world["project"], [world["memory"]]))[0]
        await db.commit()
    async with app.state.sessionmaker() as db:
        await db.execute(update(StoryMemory).where(StoryMemory.id == world["memory_ref"]).values(content="新记忆"))
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert not await _store(db, [snapshot], [vector], config)
        assert (await db.get(RetrievalChunk, world["memory"])).embedding is None
        await db.execute(update(StoryMemory).where(StoryMemory.id == world["memory_ref"]).values(content="真实记忆"))
        await db.commit()
        snapshot = (await rs.snapshot_embedding_inputs(db, world["project"], [world["memory"]]))[0]
        await db.commit()
    async with app.state.sessionmaker() as db:
        await db.execute(update(RetrievalChunk).where(RetrievalChunk.id == world["memory"]).values(
            embedding=b"new-result", embedding_key="other:4",
        ))
        await db.commit()
        assert not await _store(db, [snapshot], [vector], config)
        row = await db.get(RetrievalChunk, world["memory"])
        assert row.embedding == b"new-result" and row.embedding_key == "other:4"
        await db.rollback()
    async with app.state.sessionmaker() as db:
        snapshot = (await rs.snapshot_embedding_inputs(db, world["project"], [world["memory"]]))[0]
        assert await _store(db, [snapshot], [vector], config)
        await db.commit()
        row = await db.get(RetrievalChunk, world["memory"])
        await db.refresh(row)
        assert row.embedding_key == key and row.version == snapshot.version + 1


async def test_orphan_requires_explicit_snapshot_and_cas_delete(app, world):
    async with app.state.sessionmaker() as db:
        await db.execute(delete(StoryMemory).where(StoryMemory.id == world["memory_ref"]))
        await db.commit()
        with pytest.raises(ConflictError):
            await rs.snapshot_embedding_inputs(db, world["project"], [world["memory"]])
        snapshot = (await rs.snapshot_embedding_inputs(
            db, world["project"], [world["memory"]], allow_orphans=True,
        ))[0]
        assert snapshot.embed_text is None
        await db.execute(update(RetrievalChunk).where(RetrievalChunk.id == world["memory"]).values(text="新索引"))
        await db.commit()
        assert not await rs.delete_orphan_snapshot(db, snapshot)
        snapshot = (await rs.snapshot_embedding_inputs(
            db, world["project"], [world["memory"]], allow_orphans=True,
        ))[0]
        assert await rs.delete_orphan_snapshot(db, snapshot)
        await db.commit()
        assert await db.get(RetrievalChunk, world["memory"]) is None


async def test_combined_guard_rechecks_all_callbacks_even_on_cache_hit(app, world):
    fake = FakeLLM()
    calls = []
    denied = False

    async def authorize(_db):
        calls.append("authorize")
        if denied:
            raise ForbiddenError("不得暴露的错误")

    async def verify(_db):
        calls.append("verify")

    async with app.state.sessionmaker() as db:
        stamp = await read_visibility_stamp(db, world["project"])
    embedder = rs.Embedder(fake.factory("openai_compat", "key", None, 1), "openai_compat", "model")
    guarded = rs.guard_embedder(embedder, stamp, app.state.sessionmaker, authorize=authorize)
    guarded = rs.guard_embedder(guarded, stamp, app.state.sessionmaker, verify=verify)
    await guarded.embed(["检索"])
    assert calls == ["authorize", "verify"] and len(fake.embed_calls) == 1
    denied = True
    with pytest.raises(VisibilityPolicyError):
        await guarded.embed(["检索"])
    assert len(fake.embed_calls) == 1


async def test_wrapped_policy_failure_is_never_keyword_fallback():
    fake = FakeLLM()

    async def failed(_texts, _model):
        try:
            raise RequestPolicyError("private")
        except RequestPolicyError as exc:
            raise AIProviderError("connection failed") from exc

    provider = fake.factory("openai_compat", "key", None, 1)
    provider.embed = failed
    embedder = rs.Embedder(provider, "openai_compat", "model")
    with pytest.raises(VisibilityPolicyError) as caught:
        await rs.embed_query(embedder, "检索")
    assert "private" not in str(caught.value)


async def test_batch_stale_source_does_not_write_any_vector_and_success_is_atomic(app, world):
    vectors = [np.array([1.0, 0.0], dtype=np.float32), np.array([0.0, 1.0], dtype=np.float32)]
    async with app.state.sessionmaker() as db:
        selected = await project_ai_config.resolve_shared_embedding(
            db, world["owner"], world["project"], cipher=app.state.cipher,
        )
        config = await rs.load_bound_shared_embedding_config(db, selected, cipher=app.state.cipher)
        snapshots = await rs.snapshot_embedding_inputs(db, world["project"], [world["kb"], world["memory"]])
        await db.commit()
    async with app.state.sessionmaker() as db:
        await db.execute(update(StoryMemory).where(StoryMemory.id == world["memory_ref"]).values(content="新内容"))
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert not await _store(db, snapshots, vectors, config)
        rows = (await db.execute(select(RetrievalChunk.embedding, RetrievalChunk.embedding_key).where(
            RetrievalChunk.id.in_([world["kb"], world["memory"]]),
        ))).all()
        assert rows == [(None, None), (None, None)]
        await db.rollback()
    async with app.state.sessionmaker() as db:
        await db.execute(update(StoryMemory).where(StoryMemory.id == world["memory_ref"]).values(content="真实记忆"))
        await db.commit()
        snapshots = await rs.snapshot_embedding_inputs(db, world["project"], [world["kb"], world["memory"]])
        assert await _store(db, snapshots, vectors, config)
        await db.commit()
        rows = (await db.execute(select(RetrievalChunk.embedding, RetrievalChunk.embedding_key).where(
            RetrievalChunk.id.in_([world["kb"], world["memory"]]),
        ).order_by(RetrievalChunk.id))).all()
        assert all(blob is not None and key == rs.make_embedding_key(OWNER_SPACE, 2)
                   for blob, key in rows)


async def test_batch_does_not_flush_unrelated_draft_and_root_rollback_reverts_vectors(app, world):
    async with app.state.sessionmaker() as db:
        selected = await project_ai_config.resolve_shared_embedding(
            db, world["owner"], world["project"], cipher=app.state.cipher,
        )
        config = await rs.load_bound_shared_embedding_config(db, selected, cipher=app.state.cipher)
        snapshot = (await rs.snapshot_embedding_inputs(db, world["project"], [world["memory"]]))[0]
        await db.commit()
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, world["project"])
        project.title = "未授权保存的草稿"
        assert await _store(db, [snapshot], [np.array([1.0, 0.0])], config)
        with db.no_autoflush:
            title = await db.scalar(select(Project.__table__.c.title).where(Project.id == world["project"]))
        assert title != "未授权保存的草稿"
        assert project.title == "未授权保存的草稿"
        await db.rollback()
    async with app.state.sessionmaker() as db:
        row = await db.get(RetrievalChunk, world["memory"])
        assert row.embedding is None and row.embedding_key is None
        assert (await db.get(Project, world["project"])).title != "未授权保存的草稿"


@pytest.mark.parametrize("change", ["model", "api_key"])
async def test_changed_author_model_fails_closed_before_vector_write(app, world, change):
    """原 shared:v2 键用例中「配置换密钥即不同空间」的价值保留在此：绑定配置任一身份字段变化都失败关闭。"""
    async with app.state.sessionmaker() as db:
        selected = await project_ai_config.resolve_shared_embedding(
            db, world["owner"], world["project"], cipher=app.state.cipher,
        )
        config = await rs.load_bound_shared_embedding_config(db, selected, cipher=app.state.cipher)
        snapshot = (await rs.snapshot_embedding_inputs(db, world["project"], [world["memory"]]))[0]
        await db.commit()
    async with app.state.sessionmaker() as db:
        values = ({"model": "new-space"} if change == "model"
                  else {"api_key_enc": app.state.cipher.encrypt("new-key")})
        await db.execute(update(AIConfig).where(AIConfig.id == world["config"]).values(**values))
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(VisibilityPolicyError):
            await _store(db, [snapshot], [np.array([1.0, 0.0])], config)
        await db.rollback()
        row = await db.get(RetrievalChunk, world["memory"])
        assert row.embedding is None and row.embedding_key is None


async def test_orphan_is_not_deleted_when_source_reappears(app, world):
    async with app.state.sessionmaker() as db:
        await db.execute(delete(StoryMemory).where(StoryMemory.id == world["memory_ref"]))
        await db.commit()
        snapshot = (await rs.snapshot_embedding_inputs(
            db, world["project"], [world["memory"]], allow_orphans=True,
        ))[0]
        db.add(StoryMemory(id=world["memory_ref"], project_id=world["project"], chapter_no=1,
                           kind="manual", content="新的有效来源"))
        await db.flush()
        assert not await rs.delete_orphan_snapshot(db, snapshot)
        await db.rollback()


async def test_guard_storage_failure_cannot_fall_back_or_hit_cached_embedding(app, world, monkeypatch):
    fake = FakeLLM()
    async with app.state.sessionmaker() as db:
        stamp = await read_visibility_stamp(db, world["project"])
    guarded = rs.guard_embedder(
        rs.Embedder(fake.factory("openai_compat", "key", None, 1), "openai_compat", "model"),
        stamp, app.state.sessionmaker,
    )
    await guarded.embed(["检索"])
    assert len(fake.embed_calls) == 1

    async def unavailable(*_args):
        raise RuntimeError("数据库内部敏感信息")

    monkeypatch.setattr("app.services.ai_dispatch.require_visibility_stamp", unavailable)
    with pytest.raises(VisibilityPolicyError) as caught:
        await rs.embed_query(guarded, "检索")
    assert "数据库内部敏感信息" not in str(caught.value)
    assert len(fake.embed_calls) == 1


async def test_batch_rolls_back_all_rows_if_source_changes_after_snapshot_check(app, world, monkeypatch):
    async with app.state.sessionmaker() as db:
        selected = await project_ai_config.resolve_shared_embedding(
            db, world["owner"], world["project"], cipher=app.state.cipher,
        )
        config = await rs.load_bound_shared_embedding_config(db, selected, cipher=app.state.cipher)
        snapshots = await rs.snapshot_embedding_inputs(db, world["project"], [world["kb"], world["memory"]])
        await db.commit()
    original = rs.snapshot_embedding_inputs

    async def concurrent_change(db, project_id, ids, *, allow_orphans=False):
        fresh = await original(db, project_id, ids, allow_orphans=allow_orphans)
        await db.execute(update(StoryMemory).where(StoryMemory.id == world["memory_ref"]).values(content="发生竞争"))
        return fresh

    monkeypatch.setattr(rs, "snapshot_embedding_inputs", concurrent_change)
    async with app.state.sessionmaker() as db:
        assert not await _store(
            db, snapshots, [np.array([1.0, 0.0]), np.array([0.0, 1.0])], config,
        )
        rows = (await db.execute(select(RetrievalChunk.embedding).where(
            RetrievalChunk.id.in_([world["kb"], world["memory"]]),
        ))).scalars().all()
        assert rows == [None, None]
        await db.rollback()


async def test_real_embedding_http_combines_guards_and_stops_before_send(app, world):
    async with app.state.sessionmaker() as db:
        stamp = await read_visibility_stamp(db, world["project"])
    allowed = False
    checks = []

    async def authorize(_db):
        checks.append("actor")

    async def verify(_db):
        checks.append("model")
        if not allowed:
            raise ForbiddenError("作者私有配置不得外显")

    payload = json.dumps({"object": "list", "model": "model", "data": [{"index": 0, "embedding": [1.0, 0.0]}]})
    async with _server(payload=payload.encode(), mime="application/json") as (url, _, headers, body, requests):
        headers.set()
        body.set()
        provider = OpenAICompatProvider("local-key", f"{url}/v1/", 3)
        guarded = rs.guard_embedder(rs.Embedder(provider, "openai_compat", "model"), stamp,
                                    app.state.sessionmaker, authorize=authorize)
        guarded = rs.guard_embedder(guarded, stamp, app.state.sessionmaker, verify=verify)
        with pytest.raises(VisibilityPolicyError) as caught:
            await rs.embed_query(guarded, "机密数据")
        assert "作者私有配置" not in str(caught.value)
        assert checks == ["actor", "model"] and not requests
        allowed = True
        vector, warning = await rs.embed_query(guarded, "机密数据")
        assert warning == "" and vector is not None and len(requests) == 1
        assert checks == ["actor", "model", "actor", "model"]


async def test_authorization_timeout_is_fatal_but_provider_timeout_can_use_keywords(app, world, monkeypatch):
    async with app.state.sessionmaker() as db:
        stamp = await read_visibility_stamp(db, world["project"])
    monkeypatch.setattr(rs, "QUERY_EMBED_TIMEOUT_SECONDS", 0.02)
    fake = FakeLLM()

    async def blocked_authorization(_db):
        await asyncio.Event().wait()

    guarded = rs.guard_embedder(
        rs.Embedder(fake.factory("openai_compat", "key", None, 1), "openai_compat", "model"),
        stamp, app.state.sessionmaker, authorize=blocked_authorization,
    )
    with pytest.raises(VisibilityPolicyError):
        await rs.embed_query(guarded, "检索")
    assert not fake.embed_calls

    provider = fake.factory("openai_compat", "key", None, 1)

    async def slow_provider(_texts, _model):
        # 替身现在模拟 hooks；先通过预检，再模拟 provider 的普通超时。
        await provider._preflight()
        raise TimeoutError

    monkeypatch.setattr(rs, "QUERY_EMBED_TIMEOUT_SECONDS", 3)
    provider.embed = slow_provider
    guarded = rs.guard_embedder(rs.Embedder(provider, "openai_compat", "model"), stamp, app.state.sessionmaker)
    vector, warning = await rs.embed_query(guarded, "检索")
    assert vector is None and "超时" in warning


async def test_upsert_rejects_existing_reference_of_another_project_before_flush(app, world):
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, world["foreign_project"])
        project.title = "不应提前写入"
        with pytest.raises(ConflictError) as caught:
            await rs.upsert_many(
                db, world["foreign_project"], "kb", [rs.ChunkInput(ref_id=world["kb_ref"], text="不应覆盖")],
            )
        assert "不应覆盖" not in str(caught.value)
        with db.no_autoflush:
            title = await db.scalar(select(Project.__table__.c.title).where(Project.id == world["foreign_project"]))
            indexed = await db.scalar(select(RetrievalChunk.text).where(RetrievalChunk.id == world["kb"]))
        assert title != "不应提前写入" and indexed == "标题 0\n正文 0"
        await db.rollback()


async def test_project_scoped_flags_and_delete_reject_foreign_source_atomically(app, world):
    async with app.state.sessionmaker() as db:
        ids = [world["kb_ref"], world["foreign_chunk"]]
        with pytest.raises(ConflictError) as caught:
            await rs.set_project_flags(db, world["project"], "kb", ids, enabled=False)
        assert str(world["foreign_chunk"]) not in str(caught.value)
        with pytest.raises(ConflictError):
            await rs.delete_project_refs(db, world["project"], "kb", ids)
        row = await db.get(RetrievalChunk, world["kb"])
        assert row is not None and row.enabled is True
        await rs.set_project_flags(db, world["project"], "kb", [world["kb_ref"]], enabled=False)
        await db.commit()
        await db.refresh(row)
        assert row.enabled is False
        await rs.delete_project_refs(db, world["project"], "kb", [world["kb_ref"]])
        await db.commit()
        assert await db.scalar(select(RetrievalChunk.id).where(RetrievalChunk.id == world["kb"])) is None


async def test_project_scoped_validation_rejects_misassigned_index_row(app, world):
    async with app.state.sessionmaker() as db:
        await db.execute(update(RetrievalChunk).where(RetrievalChunk.id == world["memory"]).values(
            project_id=world["foreign_project"],
        ))
        await db.commit()
        with pytest.raises(ConflictError):
            await rs.validate_project_source_refs(db, world["project"], "memory", [world["memory_ref"]])
        with pytest.raises(ConflictError):
            await rs.delete_project_refs(db, world["project"], "memory", [world["memory_ref"]])
        assert await db.get(RetrievalChunk, world["memory"]) is not None


@pytest.mark.parametrize("bad_id", [True, 0, -1, 2**31, None, []])
async def test_source_validation_rejects_invalid_database_identity_without_query(app, world, bad_id):
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await rs.snapshot_embedding_inputs(db, world["project"], [bad_id])
        with pytest.raises(ConflictError):
            await rs.validate_project_source_refs(db, world["project"], "memory", [bad_id])


async def test_exhausted_retrieval_version_rejects_batch_without_partial_mutation(app, world):
    async with app.state.sessionmaker() as db:
        await db.execute(update(RetrievalChunk).where(RetrievalChunk.id == world["memory"]).values(
            version=2**31 - 1,
        ))
        await db.commit()
        selection = await project_ai_config.resolve_shared_embedding(
            db, world["owner"], world["project"], cipher=app.state.cipher,
        )
        config = await rs.load_bound_shared_embedding_config(db, selection, cipher=app.state.cipher)
        snapshot = (await rs.snapshot_embedding_inputs(db, world["project"], [world["memory"]]))[0]
        assert not await _store(db, [snapshot], [np.array([1.0, 0.0])], config)
        with pytest.raises(ConflictError):
            await rs.set_project_flags(db, world["project"], "memory", [world["memory_ref"]], enabled=False)
        await db.rollback()
        row = await db.get(RetrievalChunk, world["memory"])
        assert row.version == 2**31 - 1 and row.embedding is None and row.enabled


async def test_revocation_and_reinvitation_do_not_reuse_old_vector_selection(app, world):
    async with app.state.sessionmaker() as db:
        editor = User(username="retrieval-editor", password_hash="x")
        db.add(editor)
        await db.flush()
        member = ProjectCollaborator(project_id=world["project"], user_id=editor.id, role="editor",
                                     status="accepted", allow_owner_ai=True)
        db.add(member)
        await db.commit()
        editor_id, member_id = editor.id, member.id
        selection = await project_ai_config.resolve_shared_embedding(
            db, editor_id, world["project"], cipher=app.state.cipher,
        )
        config = await rs.load_bound_shared_embedding_config(db, selection, cipher=app.state.cipher)
        snapshot = (await rs.snapshot_embedding_inputs(db, world["project"], [world["memory"]]))[0]
        stamp = await read_visibility_stamp(db, world["project"])
        await db.commit()
    async with app.state.sessionmaker() as db:
        member = await db.get(ProjectCollaborator, member_id)
        member.status, member.revision = "revoked", member.revision + 1
        await db.commit()
    fake = FakeLLM()

    async def verify(db):
        await project_ai_config.revalidate_shared_embedding(db, selection, cipher=app.state.cipher)

    guarded = rs.guard_embedder(
        rs.Embedder(fake.factory("openai_compat", "key", None, 1), "openai_compat", "model"),
        stamp, app.state.sessionmaker, verify=verify,
    )
    with pytest.raises(VisibilityPolicyError):
        await guarded.embed([snapshot.embed_text])
    assert not fake.embed_calls
    async with app.state.sessionmaker() as db:
        with pytest.raises(VisibilityPolicyError):
            await _store(db, [snapshot], [np.array([1.0, 0.0])], config)
        await db.rollback()
        member = await db.get(ProjectCollaborator, member_id)
        member.status, member.invitation_id, member.revision = "accepted", str(uuid4()), member.revision + 1
        await db.commit()
        with pytest.raises(VisibilityPolicyError):
            await _store(db, [snapshot], [np.array([1.0, 0.0])], config)
        await db.rollback()
        assert (await db.get(RetrievalChunk, world["memory"])).embedding is None
