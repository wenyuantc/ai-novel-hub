"""共享向量缓存必须通过与章节发送同一物理 lease 的短预检。"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update

from app.ai.providers import create_provider
from app.core.security import ACCESS_TOKEN_COOKIE, decode_access_token
from app.models import AIConfig, Chapter, Project, ProjectCollaborator, Task, User
from app.services import chapter_claim, project_access, project_ai_config
from app.services import collaboration_ai_guard as send_guard
from app.services import retrieval_service as rs
from app.services.ai_dispatch import read_visibility_stamp
from app.services.authorization_service import AuthenticatedActor
from app.services.collaboration_task_access import bind_task_grant
from app.services.task_engine import execution_identity
from tests.helpers import register
from tests.test_ai_visibility_dispatch import _server


async def _make_world(app, url, *, after_verify=None, verify_extra=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        actor_out = await register(client, "cache-editor")
        assert (await client.get("/api/v1/auth/me")).status_code == 200
        authenticated = decode_access_token(client.cookies[ACCESS_TOKEN_COOKIE], app.state.settings.jwt_secret)
        assert authenticated is not None and authenticated[0] == actor_out["id"]
    async with app.state.sessionmaker() as db:
        owner = User(username="cache-owner", password_hash="x")
        actor = await db.get(User, authenticated[0])
        db.add(owner)
        await db.flush()
        project = Project(user_id=owner.id, title="共享向量作品")
        db.add(project)
        await db.flush()
        member = ProjectCollaborator(project_id=project.id, user_id=actor.id, status="accepted", role="editor",
                                     allow_owner_ai=True)
        config = AIConfig(user_id=owner.id, name="内部配置", provider="openai_compat", model="embedding-model",
                          base_url=f"{url}/v1/", model_type="embedding", is_default=True,
                          api_key_enc=app.state.cipher.encrypt("local-key"))
        chapter = Chapter(project_id=project.id, title="待生成章节", content="正文", status="draft")
        db.add_all([member, config, chapter])
        await db.commit()
        selection = await project_ai_config.resolve_shared_embedding(
            db, actor.id, project.id, cipher=app.state.cipher, allow_private=True, require_vector=True,
        )
        stamp = await read_visibility_stamp(db, project.id)
        db.info["authenticated_actor"] = AuthenticatedActor(authenticated[0], authenticated[1])
        await project_access.require_project(db, actor.id, project.id, "ai.use", for_update=True,
                                             visibility_write=True, grant_identity=selection.grant_identity)
        payload = bind_task_grant({"request": {"project_id": project.id}, "chapter_ids": [chapter.id]},
                                  selection.grant_identity)
        payload[send_guard.EMBEDDING_KEY] = project_ai_config.serialize_shared_embedding(selection)
        payload["_task_actor_token_version"] = authenticated[1]
        task = Task(user_id=actor.id, project_id=project.id, kind="chapter.batch", status="queued",
                    resource_key=f"batch:{project.id}", statistics_key=str(uuid4()),
                    queued_at=datetime.now(UTC), input=payload,
                    checkpoint={"phase": "generate", "current": None, "items": [{"chapter_id": chapter.id}]})
        db.add(task)
        await db.flush()
        queued = await db.scalar(select(Task).where(Task.id == task.id).with_for_update())
        execution_key = str(uuid4())
        identity = execution_identity.from_queued_task(queued, execution_key)
        changed = await db.execute(update(Task).where(
            Task.id == identity.task_id, Task.statistics_key == identity.task_key, Task.status == "queued",
            Task.execution_key.is_(None), Task.queued_at == identity.queued_at,
        ).values(status="running", execution_key=execution_key))
        assert changed.rowcount == 1
        attempt_key = str(uuid4())
        task.checkpoint = {**task.checkpoint, "_batch_attempt": attempt_key}
        await db.flush()
        claim = await chapter_claim.claim_batch_chapter(
            db, task_id=task.id, task_key=task.statistics_key, actor_id=actor.id, project_id=project.id,
            chapter_id=chapter.id, queued_at=task.queued_at, grant_identity=selection.grant_identity,
            execution_key=execution_key,
        )
        attempt = send_guard.BatchAttemptIdentity(claim, 0, attempt_key)
        await db.commit()
        checks = send_guard.for_task(
            identity, selection, stamp, cipher=app.state.cipher, allow_private=True, chapter=claim,
            batch_attempt=attempt,
            after_verify=after_verify,
        )
        world = {"project": project.id, "actor": actor.id, "owner": owner.id, "chapter": chapter.id,
                 "task": task.id, "task_object": identity, "config": config.id, "stamp": stamp,
                 "claim": claim, "batch_attempt": attempt, "selection": selection}
    bound = await checks.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=3)
    embedder = rs.Embedder(bound.provider, bound.provider_name, bound.model)
    world["bound_provider"] = bound.provider
    world["embedder"] = rs.guard_embedder(embedder, stamp, app.state.sessionmaker, verify=verify_extra)
    return world


@pytest.fixture
async def embedding_server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    payload = json.dumps({"object": "list", "model": "embedding-model",
                          "data": [{"index": 0, "embedding": [1.0, 0.0]}]}).encode()
    async with _server(payload=payload, mime="application/json") as (url, received, headers, body, requests):
        headers.set()
        body.set()
        yield url, received, headers, body, requests


async def test_cached_shared_vector_rechecks_original_task_chapter_and_binding_under_physical_lease(
    app, embedding_server,
):
    url, _, _, _, requests = embedding_server
    callbacks = []

    async def verified():
        callbacks.append("checked")

    world = await _make_world(app, url, after_verify=verified)
    guarded = world["embedder"]
    from app.services.ai_dispatch import require_chapter_transport

    require_chapter_transport(guarded.provider)
    vector, warning = await rs.embed_query(guarded, "重复查询")
    assert warning == "" and vector.tolist() == [1.0, 0.0]
    assert len(requests) == 1 and callbacks == ["checked"]
    vector, warning = await rs.embed_query(guarded, "重复查询")
    assert warning == "" and vector.tolist() == [1.0, 0.0]
    assert len(requests) == 1 and callbacks == ["checked"]


@pytest.mark.parametrize("change", [
    "model", "provider_name", "provider", "provider_inner", "bound_model", "provider_api_key",
    "transport", "authorize", "verify", "verify_preflight", "chapter", "dim", "cached_value", "cache_key",
])
async def test_cache_rejects_mutated_execution_binding_or_vector(app, embedding_server, fake_llm, change):
    import numpy as np

    url, _, _, _, requests = embedding_server
    world = await _make_world(app, url)
    embedder = world["embedder"]
    await embedder.embed(["原查询"])
    assert "原查询" not in repr(embedder) and "embedding-model" not in repr(embedder)
    if change == "model":
        embedder.model = "伪造模型"
    elif change == "provider_name":
        embedder.provider_name = "伪造提供方"
    elif change == "provider":
        embedder.provider = fake_llm.factory("openai_compat", "other", None, 1)
    elif change == "provider_inner":
        embedder.provider._provider = fake_llm.factory("openai_compat", "other", None, 1)
    elif change == "bound_model":
        embedder.provider._provider._model = "伪造模型"
    elif change == "provider_api_key":
        embedder.provider._provider.api_key = "伪造凭据"
    elif change == "transport":
        embedder.provider._provider._provider = fake_llm.factory("openai_compat", "other", None, 1)
    elif change == "authorize":
        embedder.provider._authorize = None
    elif change == "verify":
        embedder.provider._verify = None
    elif change == "verify_preflight":
        embedder.provider._verify_preflight = None
    elif change == "chapter":
        embedder.provider._chapter = None
    elif change == "dim":
        embedder.dim += 1
    elif change == "cached_value":
        embedder._cache["原查询"][0] = 0.25
    else:
        embedder._cache["伪造缓存"] = np.array([0.0, 1.0], dtype=np.float32)
    with pytest.raises(rs.VisibilityPolicyError):
        await rs.embed_query(embedder, "伪造缓存" if change == "cache_key" else "原查询")
    assert len(requests) == 1 and fake_llm.embed_calls == []


@pytest.mark.parametrize("change", [
    "revoke", "downgrade", "new_invitation", "owner_grant", "inactive_actor", "config", "task_execution",
    "task_status", "task_binding", "chapter_claim", "chapter_status", "visibility",
])
async def test_cached_vector_fails_closed_after_original_guard_changes(app, embedding_server, change):
    from sqlalchemy import select, update

    url, _, _, _, requests = embedding_server
    world = await _make_world(app, url)
    embedder = world["embedder"]
    await embedder.embed(["原始检索正文"])
    async with app.state.sessionmaker() as db:
        if change in ("revoke", "downgrade", "new_invitation", "owner_grant"):
            member = await db.scalar(select(ProjectCollaborator).where(
                ProjectCollaborator.project_id == world["project"], ProjectCollaborator.user_id == world["actor"],
            ))
            if change == "revoke":
                member.status = "revoked"
            elif change == "downgrade":
                member.role = "viewer"
            elif change == "new_invitation":
                member.invitation_id, member.revision = str(uuid4()), member.revision + 1
            else:
                member.allow_owner_ai = False
        elif change == "inactive_actor":
            (await db.get(User, world["actor"])).is_active = False
        elif change == "config":
            (await db.get(AIConfig, world["config"])).model = "敏感新模型"
        elif change == "task_execution":
            (await db.get(Task, world["task"])).execution_key = str(uuid4())
        elif change == "task_status":
            (await db.get(Task, world["task"])).status = "cancelled"
        elif change == "task_binding":
            task = await db.get(Task, world["task"])
            task.input = {**task.input, send_guard.EMBEDDING_KEY: {"secret": "敏感配置"}}
        elif change == "chapter_claim":
            (await db.get(Chapter, world["chapter"])).generation_claim_key = str(uuid4())
        elif change == "chapter_status":
            chapter = await db.get(Chapter, world["chapter"])
            chapter.status = "draft"
            chapter.generation_claim_key = None
            chapter.generation_claim_task_key = None
            chapter.generation_claim_prev_status = None
        else:
            await db.execute(update(Project).where(Project.id == world["project"]).values(
                ai_visibility_revision=Project.ai_visibility_revision + 1,
            ))
        await db.commit()
    with pytest.raises(rs.VisibilityPolicyError) as caught:
        await rs.embed_query(embedder, "原始检索正文")
    assert "敏感" not in str(caught.value) and "原始检索正文" not in str(caught.value)
    assert len(requests) == 1


@pytest.mark.parametrize("change", ["model", "provider", "verify"])
async def test_changed_binding_before_cache_miss_never_reaches_provider(app, embedding_server, fake_llm, change):
    url, _, _, _, requests = embedding_server
    world = await _make_world(app, url)
    embedder = world["embedder"]
    await embedder.embed(["首次查询"])
    if change == "model":
        embedder.model = "伪造模型"
    elif change == "provider":
        embedder.provider = fake_llm.factory("openai_compat", "other", None, 1)
    else:
        embedder.provider._verify = None
    with pytest.raises(rs.VisibilityPolicyError):
        await rs.embed_query(embedder, "新查询")
    assert len(requests) == 1 and fake_llm.embed_calls == []


async def test_model_change_while_network_in_flight_cannot_write_cache(app, embedding_server):
    import asyncio

    url, received, headers, body, requests = embedding_server
    world = await _make_world(app, url)
    embedder = world["embedder"]
    received.clear()
    headers.clear()
    body.clear()
    pending = asyncio.create_task(embedder.embed(["网络查询"]))
    try:
        await asyncio.wait_for(received.wait(), 3)
        embedder.model = "运行中被换掉的模型"
        headers.set()
        body.set()
        with pytest.raises(rs.VisibilityPolicyError):
            await asyncio.wait_for(pending, 3)
        assert "网络查询" not in embedder._cache and len(requests) == 1
    finally:
        headers.set()
        body.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_cached_vector_is_not_shared_mutable_result(app, embedding_server):
    url, _, _, _, requests = embedding_server
    world = await _make_world(app, url)
    embedder = world["embedder"]
    vector = (await embedder.embed(["原查询"]))[0]
    vector[0] = 0.25
    assert (await embedder.embed(["原查询"]))[0].tolist() == [1.0, 0.0]
    assert len(requests) == 1


@pytest.mark.parametrize("change", ["model", "cache"])
async def test_cached_vector_checks_binding_again_after_waiting_for_preflight(app, embedding_server, change):
    import asyncio

    url, _, _, _, requests = embedding_server
    reached, release = asyncio.Event(), asyncio.Event()
    armed = False

    async def verified(_db):
        if armed:
            reached.set()
            await release.wait()

    world = await _make_world(app, url, verify_extra=verified)
    embedder = world["embedder"]
    await embedder.embed(["原查询"])
    armed = True
    pending = asyncio.create_task(rs.embed_query(embedder, "原查询"))
    try:
        await asyncio.wait_for(reached.wait(), 3)
        if change == "model":
            embedder.model = "伪造模型"
        else:
            embedder._cache["原查询"][0] = 0.25
        release.set()
        with pytest.raises(rs.VisibilityPolicyError):
            await asyncio.wait_for(pending, 3)
        assert len(requests) == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_revoke_between_distinct_network_batches_fails_without_returning_cached_data(
    app, embedding_server, monkeypatch,
):
    import asyncio

    from sqlalchemy import update

    url, received, headers, body, requests = embedding_server
    world = await _make_world(app, url)
    embedder = world["embedder"]
    await embedder.embed(["原查询"])
    monkeypatch.setattr(rs, "EMBED_BATCH", 1)
    received.clear()
    headers.clear()
    body.clear()
    pending = asyncio.create_task(embedder.embed(["原查询", "后续第一批", "后续第二批"]))
    try:
        await asyncio.wait_for(received.wait(), 3)
        assert len(requests) == 2
        headers.set()
        async with app.state.sessionmaker() as db:
            await db.execute(update(Task).where(Task.id == world["task"]).values(execution_key=str(uuid4())))
            await db.commit()
        body.set()
        with pytest.raises(rs.VisibilityPolicyError):
            await asyncio.wait_for(pending, 3)
        assert len(requests) == 2
    finally:
        headers.set()
        body.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_cancelling_cache_preflight_releases_lease_and_cannot_return_old_cache(app, embedding_server):
    import asyncio

    from sqlalchemy import update

    url, _, _, _, requests = embedding_server
    reached, release = asyncio.Event(), asyncio.Event()
    armed = False

    async def verified(_db):
        if armed:
            reached.set()
            await release.wait()

    world = await _make_world(app, url, verify_extra=verified)
    embedder = world["embedder"]
    await embedder.embed(["原查询"])
    armed = True
    pending = asyncio.create_task(rs.embed_query(embedder, "原查询"))
    try:
        await asyncio.wait_for(reached.wait(), 3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        async with app.state.sessionmaker() as db:
            await asyncio.wait_for(db.execute(update(Project).where(Project.id == world["project"]).values(
                ai_visibility_revision=Project.ai_visibility_revision + 1,
            )), 3)
            await db.commit()
        with pytest.raises(rs.VisibilityPolicyError):
            await rs.embed_query(embedder, "原查询")
        assert len(requests) == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_cache_preflight_db_failure_never_degrades_to_keyword(app, embedding_server, monkeypatch):
    from app.services import ai_dispatch

    url, _, _, _, requests = embedding_server
    world = await _make_world(app, url)
    embedder = world["embedder"]
    await embedder.embed(["原查询"])

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("敏感数据库凭据")

    monkeypatch.setattr(ai_dispatch, "_lock", unavailable)
    with pytest.raises(rs.VisibilityPolicyError) as caught:
        await rs.embed_query(embedder, "原查询")
    assert "敏感数据库凭据" not in str(caught.value)
    assert len(requests) == 1


async def test_cross_task_cannot_use_parent_cache_lease(app, embedding_server):
    import asyncio

    from app.services import ai_dispatch

    url, _, _, _, requests = embedding_server
    world = await _make_world(app, url)
    armed = False

    async def wrong_task(db):
        if armed:
            await asyncio.create_task(ai_dispatch._require_send_lease(db, world["actor"], world["project"]))

    embedder = rs.guard_embedder(world["embedder"], world["stamp"], app.state.sessionmaker, verify=wrong_task)
    await embedder.embed(["原查询"])
    armed = True
    with pytest.raises(rs.VisibilityPolicyError):
        await rs.embed_query(embedder, "原查询")
    assert len(requests) == 1


async def test_fake_without_real_http_hooks_cannot_supply_chapter_send_proof(app, embedding_server, fake_llm):
    from app.services import ai_dispatch

    url, _, _, _, _ = embedding_server
    world = await _make_world(app, url)

    async def authorize(db):
        await ai_dispatch.authorize_send_actor(db, world["actor"])

    async def verify(db):
        await chapter_claim.require_sending_chapter(db, world["claim"])

    fake = fake_llm.factory("openai_compat", "fake", None, 1)
    embedder = rs.guard_embedder(rs.Embedder(fake, "openai_compat", "embedding-model"), world["stamp"],
                                 app.state.sessionmaker, authorize=authorize, verify=verify)
    with pytest.raises(rs.VisibilityPolicyError):
        await embedder.embed(["不得发送"])
    assert fake_llm.embed_calls == []


async def test_rewrapping_cannot_change_or_reassign_chapter_claim(app, embedding_server):
    from dataclasses import replace

    url, _, _, _, requests = embedding_server
    world = await _make_world(app, url)
    original = world["embedder"]
    await original.embed(["原查询"])
    forged = replace(world["claim"], chapter_id=world["claim"].chapter_id + 1)
    with pytest.raises(rs.VisibilityPolicyError):
        rs.guard_embedder(original, world["stamp"], app.state.sessionmaker, chapter=forged)
    old_wrapper = world["bound_provider"]
    old_wrapper._chapter = forged
    raw = rs.Embedder(old_wrapper, "openai_compat", "embedding-model")
    with pytest.raises(rs.VisibilityPolicyError):
        rs.guard_embedder(raw, world["stamp"], app.state.sessionmaker)
    assert len(requests) == 1


async def test_untrusted_provider_cannot_claim_chapter_transport(app, embedding_server, fake_llm):
    from app.services import ai_dispatch

    url, _, _, _, _ = embedding_server
    world = await _make_world(app, url)
    fake = fake_llm.factory("openai_compat", "fake", None, 1)
    with pytest.raises(rs.VisibilityPolicyError):
        ai_dispatch.guard_chapter_provider(fake, world["stamp"], app.state.sessionmaker, chapter=world["claim"])
    with pytest.raises(rs.VisibilityPolicyError):
        rs.guard_embedder(rs.Embedder(fake, "openai_compat", "embedding-model"), world["stamp"],
                          app.state.sessionmaker, chapter=world["claim"])
    with pytest.raises(rs.VisibilityPolicyError):
        rs.guard_embedder(None, world["stamp"], app.state.sessionmaker, chapter=world["claim"])
    assert fake_llm.embed_calls == []


async def test_cache_preflight_physically_blocks_a_competing_sqlite_write(app, embedding_server):
    import asyncio

    from sqlalchemy import text, update
    from sqlalchemy.exc import OperationalError

    url, _, _, _, requests = embedding_server
    reached, release = asyncio.Event(), asyncio.Event()
    armed = False

    async def verified(_db):
        if armed:
            reached.set()
            await release.wait()

    world = await _make_world(app, url, verify_extra=verified)
    embedder = world["embedder"]
    await embedder.embed(["原查询"])
    armed = True
    pending = asyncio.create_task(rs.embed_query(embedder, "原查询"))
    try:
        await asyncio.wait_for(reached.wait(), 3)
        async with app.state.sessionmaker() as db:
            await db.execute(text("PRAGMA busy_timeout = 100"))
            with pytest.raises(OperationalError):
                await db.execute(update(Project).where(Project.id == world["project"]).values(title="等待门禁"))
            await db.rollback()
        assert not pending.done()
        release.set()
        vector, warning = await asyncio.wait_for(pending, 3)
        assert warning == "" and vector.tolist() == [1.0, 0.0] and len(requests) == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def _seed_memory(app, project_id):
    from app.models import StoryMemory

    async with app.state.sessionmaker() as db:
        memory = StoryMemory(project_id=project_id, chapter_no=1, kind="manual", content="需要保护的青鸿记忆")
        db.add(memory)
        await db.flush()
        await rs.upsert_many(db, project_id, "memory", [rs.ChunkInput(ref_id=memory.id, text=memory.content)])
        await db.commit()


async def test_stale_index_cannot_return_keyword_content_after_revoke(app, embedding_server):
    from sqlalchemy import update

    url, _, _, _, requests = embedding_server
    world = await _make_world(app, url)
    await _seed_memory(app, world["project"])
    embedder = world["embedder"]
    await embedder.embed(["青鸿"])
    async with app.state.sessionmaker() as db:
        initial = await rs.search(db, world["project"], "memory", "青鸿", top_k=5, min_score=0, embedder=embedder)
        assert len(initial.hits) == 1 and initial.hits[0].text == "需要保护的青鸿记忆"
    async with app.state.sessionmaker() as db:
        await db.execute(update(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world["project"], ProjectCollaborator.user_id == world["actor"],
        ).values(status="revoked"))
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(rs.VisibilityPolicyError):
            await rs.search(db, world["project"], "memory", "青鸿", top_k=5, min_score=0, embedder=embedder)
    assert len(requests) == 1


async def test_keyword_result_rechecks_revoke_before_return(app, embedding_server, monkeypatch):
    import asyncio

    from sqlalchemy import update

    url, _, _, _, requests = embedding_server
    world = await _make_world(app, url)
    await _seed_memory(app, world["project"])
    embedder = world["embedder"]
    await embedder.embed(["青鸿"])
    reached, release = asyncio.Event(), asyncio.Event()
    load_index = rs._load_index

    async def delayed_load(*args, **kwargs):
        reached.set()
        await release.wait()
        return await load_index(*args, **kwargs)

    monkeypatch.setattr(rs, "_load_index", delayed_load)
    async with app.state.sessionmaker() as db:
        pending = asyncio.create_task(rs.search(db, world["project"], "memory", "青鸿", top_k=5, min_score=0,
                                                embedder=embedder))
        try:
            await asyncio.wait_for(reached.wait(), 3)
            async with app.state.sessionmaker() as mutator:
                await mutator.execute(update(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == world["project"],
                    ProjectCollaborator.user_id == world["actor"],
                ).values(status="revoked"))
                await mutator.commit()
            release.set()
            with pytest.raises(rs.VisibilityPolicyError):
                await asyncio.wait_for(pending, 3)
            assert len(requests) == 1
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_guarded_keyword_search_cannot_use_guard_from_another_project(app, embedding_server):
    url, _, _, _, _ = embedding_server
    world = await _make_world(app, url)
    async with app.state.sessionmaker() as db:
        foreign_owner = User(username="foreign-cache-owner", password_hash="x")
        db.add(foreign_owner)
        await db.flush()
        foreign = Project(user_id=foreign_owner.id, title="不属于协作者的作品")
        db.add(foreign)
        await db.commit()
        foreign_id = foreign.id
    await _seed_memory(app, foreign_id)
    async with app.state.sessionmaker() as db:
        with pytest.raises(rs.VisibilityPolicyError):
            await rs.search(db, foreign_id, "memory", "青鸿", top_k=5, min_score=0, embedder=world["embedder"])


async def test_cache_preflight_does_not_consume_one_shot_but_real_send_does(app, embedding_server):
    url, _, _, _, requests = embedding_server
    consumed = []

    async def once():
        if consumed:
            raise rs.VisibilityPolicyError("确认已使用", extra={"code": "section_confirmation_used"})
        consumed.append(True)

    world = await _make_world(app, url, after_verify=once)
    embedder = world["embedder"]
    await embedder.provider.preflight()
    await embedder.provider.preflight()
    assert consumed == [] and requests == []
    await embedder.embed(["原查询"])
    assert consumed == [True] and len(requests) == 1
    assert (await embedder.embed(["原查询"]))[0].tolist() == [1.0, 0.0]
    assert consumed == [True] and len(requests) == 1
    with pytest.raises(rs.VisibilityPolicyError):
        await embedder.embed(["不同查询"])
    assert len(requests) == 1


async def test_keyword_search_preflight_preserves_one_shot_for_real_send(app, embedding_server):
    url, _, _, _, requests = embedding_server
    consumed = []

    async def once():
        if consumed:
            raise rs.VisibilityPolicyError("确认已使用", extra={"code": "section_confirmation_used"})
        consumed.append(True)

    world = await _make_world(app, url, after_verify=once)
    await _seed_memory(app, world["project"])
    embedder = world["embedder"]
    async with app.state.sessionmaker() as db:
        found = await rs.search(db, world["project"], "memory", "青鸿", top_k=5, min_score=0, embedder=embedder)
        assert len(found.hits) == 1 and consumed == [] and requests == []
    await embedder.embed(["网络查询"])
    assert consumed == [True] and len(requests) == 1
    async with app.state.sessionmaker() as db:
        found = await rs.search(db, world["project"], "memory", "青鸿", top_k=5, min_score=0, embedder=embedder)
        assert len(found.hits) == 1 and consumed == [True] and len(requests) == 1


async def test_cache_lease_timeout_is_policy_failure_not_keyword_fallback(app, embedding_server, monkeypatch):
    import asyncio

    from app.services import ai_dispatch

    url, _, _, _, requests = embedding_server
    world = await _make_world(app, url)
    embedder = world["embedder"]
    await embedder.embed(["原查询"])
    monkeypatch.setattr(rs, "QUERY_EMBED_TIMEOUT_SECONDS", 0.05)

    async def blocked_lock(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(ai_dispatch, "_lock", blocked_lock)
    with pytest.raises(rs.VisibilityPolicyError):
        await rs.embed_query(embedder, "原查询")
    assert len(requests) == 1


@pytest.mark.parametrize("nested", [False, True])
async def test_rewrapping_same_guard_does_not_repeat_send_or_consume_one_shot(app, embedding_server, nested):
    url, _, _, _, requests = embedding_server
    used = []

    async def once():
        if used:
            raise rs.VisibilityPolicyError("确认已使用", extra={"code": "section_confirmation_used"})
        used.append(True)

    world = await _make_world(app, url, after_verify=once)
    bound = world["bound_provider"]
    original = world["embedder"] if nested else rs.Embedder(bound, "openai_compat", "embedding-model")
    embedder = rs.guard_embedder(original, world["stamp"], app.state.sessionmaker,
                                 authorize=bound._authorize, verify=bound._verify)
    await embedder.provider.preflight()
    assert used == []
    await embedder.embed(["第一次查询"])
    assert used == [True] and len(requests) == 1
    await embedder.embed(["第一次查询"])
    assert used == [True] and len(requests) == 1


async def test_distinct_send_guard_keeps_both_send_and_nonconsuming_preflight_checks(app, embedding_server):
    url, _, _, _, requests = embedding_server
    consumed = []

    async def once(label):
        if label in consumed:
            raise rs.VisibilityPolicyError("确认已使用", extra={"code": "section_confirmation_used"})
        consumed.append(label)

    async def original_once():
        await once("原守卫")

    async def additional_once():
        await once("附加守卫")

    world = await _make_world(app, url, after_verify=original_once)
    other = send_guard.for_task(world["task_object"], world["selection"], world["stamp"], cipher=app.state.cipher,
                                allow_private=True, chapter=world["claim"], batch_attempt=world["batch_attempt"],
                                after_verify=additional_once)
    embedder = rs.guard_embedder(world["embedder"], world["stamp"], app.state.sessionmaker,
                                 authorize=other.authorize, verify=other.verify)
    await embedder.provider.preflight()
    assert consumed == [] and requests == []
    await embedder.embed(["原查询"])
    assert consumed == ["原守卫", "附加守卫"] and len(requests) == 1
    await embedder.embed(["原查询"])
    assert consumed == ["原守卫", "附加守卫"] and len(requests) == 1
