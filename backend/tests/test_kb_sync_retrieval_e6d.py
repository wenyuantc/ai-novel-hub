"""E6-d1：同步检索与注入预览的协作授权矩阵（HTTP）。

- 检索调试（知识库 / 记忆）、章节注入预览 context-preview、HTTP 上下文（见 test_shared_http_knowledge_e6）：
  作者行为不变；已授权编辑者用作者向量（受理时固定原邀请轮次，经 for_http 受控发送，读作者旧键空间，裁决 1）；
  未授权编辑者 / 作者无向量配置只用关键词；协作者本人向量配置永不参与。
- viewer 403；待接受 / 非成员 / 已撤权 / 仅管理员 404；全部零向量调用。
- index-status 对编辑者开放精简版（裁决 8）：不返回作者配置名 / 模型名 / ID；
  含检索模式、降级原因与待建向量数（裁决 7）。
- 撤权 / 关授权发生在 prepare_provider 之后、真实发送之前：零请求，失败关闭，响应不含资料。
"""

import pytest
from sqlalchemy import select, update

from app.models import ProjectCollaborator, RetrievalChunk, StoryMemory
from app.services import collaboration_ai_guard, memory_service, retrieval_service
from tests.helpers import create_ai_config, fake_vector
from tests.test_kb_editor_entries_e6c import OWNER_KEY, _other_access, _world

QUERY = "天剑宗位于何处"


async def _author_vectors(app, project_id: int) -> None:
    """把作品全部检索单元写成作者旧键空间（与作者 / 授权编辑者索引任务产物同形）。"""
    async with app.state.sessionmaker() as db:
        for row in await db.scalars(select(RetrievalChunk).where(RetrievalChunk.project_id == project_id)):
            row.embedding = retrieval_service.encode_vector(retrieval_service.normalize_vector(fake_vector(row.text)))
            row.embedding_key = OWNER_KEY
            row.version += 1
        await db.commit()
    retrieval_service.invalidate(project_id)


async def _memory(app, project_id: int, content: str = "天剑宗掌门闭关") -> int:
    async with app.state.sessionmaker() as db:
        memory = StoryMemory(project_id=project_id, chapter_no=1, kind="manual", origin="manual", content=content)
        db.add(memory)
        await db.flush()
        await retrieval_service.upsert_project_sources(db, project_id, "memory", [memory_service._chunk(memory)])
        await db.commit()
        return memory.id


async def _chapter(client, world) -> int:
    settings = await client.patch(f"{world['base']}/kb/settings", json={"min_score": 0, "memory_min_score": 0})
    assert settings.status_code == 200, settings.text
    created = await client.post(f"{world['base']}/chapters", json={"title": "天剑宗", "content": ""})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _search(guest, world, endpoint):
    path = f"{world['base']}/kb/search" if endpoint == "kb" else f"{world['base']}/memories/search"
    return guest.post(path, json={"query": QUERY, "min_score": 0})


async def _set_allow(app, world, allow: bool) -> None:
    """直接改库切换作者模型授权、不递增 revision（只能靠租约内的配置/授权重验发现）。"""
    async with app.state.sessionmaker() as db:
        await db.execute(update(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world["project"], ProjectCollaborator.user_id == world["actor"]["id"],
        ).values(allow_owner_ai=allow))
        await db.commit()


# ---------------- 检索调试 ----------------


@pytest.mark.parametrize("endpoint", ["kb", "memory"])
async def test_authorized_editor_search_uses_owner_vectors(app, client, make_client, fake_llm, endpoint):
    editor = make_client()
    world = await _world(app, client, editor)
    await _memory(app, world["project"])
    await _author_vectors(app, world["project"])
    response = await _search(editor, world, endpoint)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "hybrid" and body["hits"]
    # 只调用作者向量模型，一次查询嵌入；协作者本人的 editor-emb 永不参与。
    assert [(call["model"], call["texts"]) for call in fake_llm.embed_calls] == [("owner-emb", [QUERY])]
    for secret in ("owner-emb", "作者向量", "editor-emb"):
        assert secret not in response.text


@pytest.mark.parametrize("case", ["unauthorized", "owner_unconfigured"])
@pytest.mark.parametrize("endpoint", ["kb", "memory"])
async def test_editor_search_keyword_only_without_shared_vectors(app, client, make_client, fake_llm, endpoint, case):
    editor = make_client()
    world = await _world(app, client, editor, allow_owner_ai=case != "unauthorized",
                         owner_embedding=case != "owner_unconfigured")
    await _memory(app, world["project"])
    if case == "unauthorized":
        await _author_vectors(app, world["project"])
    response = await _search(editor, world, endpoint)
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "keyword" and response.json()["hits"]
    assert fake_llm.embed_calls == []


async def test_author_search_unchanged(app, client, make_client, fake_llm):
    editor = make_client()
    world = await _world(app, client, editor)
    await _author_vectors(app, world["project"])
    response = await client.post(f"{world['base']}/kb/search", json={"query": QUERY, "min_score": 0})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "hybrid"
    assert [call["model"] for call in fake_llm.embed_calls] == ["owner-emb"]


@pytest.mark.parametrize(("kind", "status"), [
    ("viewer", 403), ("pending", 404), ("stranger", 404), ("revoked", 404), ("admin", 404),
])
async def test_sync_retrieval_access_matrix_zero_calls(app, client, make_client, fake_llm, kind, status):
    editor = make_client()
    world = await _world(app, client, editor)
    await _memory(app, world["project"])
    await _author_vectors(app, world["project"])
    chapter_id = await _chapter(client, world)
    guest = await _other_access(app, client, make_client, world, kind)
    for method, path, kwargs in (
        ("post", f"{world['base']}/kb/search", {"json": {"query": QUERY, "min_score": 0}}),
        ("post", f"{world['base']}/memories/search", {"json": {"query": QUERY, "min_score": 0}}),
        ("get", f"{world['base']}/kb/index-status", {}),
        ("get", f"/api/v1/chapters/{chapter_id}/context-preview", {}),
    ):
        response = await getattr(guest, method)(path, **kwargs)
        assert response.status_code == status, (path, response.text)
        assert "天剑宗位于北境" not in response.text and "owner-emb" not in response.text
    # 不存在的章节与无权章节同为 404（不披露章节是否存在）。
    if status == 404:
        missing = await guest.get("/api/v1/chapters/999999/context-preview")
        unauthorized = await guest.get(f"/api/v1/chapters/{chapter_id}/context-preview")
        assert missing.status_code == unauthorized.status_code == 404
        assert missing.json()["detail"] == unauthorized.json()["detail"]
    assert fake_llm.embed_calls == []


# ---------------- 注入预览 ----------------


@pytest.mark.parametrize("allow", [True, False])
async def test_editor_context_preview_matches_authorization(app, client, make_client, fake_llm, allow):
    """E6-d 修复：编辑者打开章节 AI 页签原先 404；现按矩阵返回，授权时用作者向量，未授权只用关键词。"""
    editor = make_client()
    world = await _world(app, client, editor, allow_owner_ai=allow)
    await _author_vectors(app, world["project"])
    chapter_id = await _chapter(client, world)
    response = await editor.get(f"/api/v1/chapters/{chapter_id}/context-preview")
    assert response.status_code == 200, response.text
    assert "天剑宗位于北境雪山之巅" in response.json()["knowledge"]
    if allow:
        assert fake_llm.embed_calls and {call["model"] for call in fake_llm.embed_calls} == {"owner-emb"}
    else:
        assert fake_llm.embed_calls == []
    # 作者路径不变：本人默认向量。
    fake_llm.embed_calls.clear()
    own = await client.get(f"/api/v1/chapters/{chapter_id}/context-preview")
    assert own.status_code == 200
    # 授权编辑者与作者同一检索空间与模型：预览一致；未授权编辑者只用关键词，结果可以不同。
    assert (own.json() == response.json()) if allow else "天剑宗位于北境雪山之巅" in own.json()["knowledge"]
    assert {call["model"] for call in fake_llm.embed_calls} == {"owner-emb"}


# ---------------- 发送前撤权 ----------------


@pytest.mark.parametrize("change", ["revoke", "owner_ai_http", "owner_ai_direct", "viewer"])
@pytest.mark.parametrize("entry", ["kb", "memory", "preview"])
async def test_revoked_after_prepare_before_send_zero_requests(app, client, make_client, fake_llm, monkeypatch,
                                                              entry, change):
    editor = make_client()
    world = await _world(app, client, editor)
    await _memory(app, world["project"])
    await _author_vectors(app, world["project"])
    chapter_id = await _chapter(client, world)
    original = collaboration_ai_guard.SendGuard.prepare_provider
    url = f"{world['base']}/collaborators/{world['actor']['id']}"

    async def change_before_send(guard, *args, **kwargs):
        bound = await original(guard, *args, **kwargs)
        async with app.state.sessionmaker() as db:
            revision = await db.scalar(select(ProjectCollaborator.revision).where(
                ProjectCollaborator.project_id == world["project"],
                ProjectCollaborator.user_id == world["actor"]["id"],
            ))
        if change == "revoke":
            changed = await client.delete(url, params={"expected_revision": revision})
            assert changed.status_code == 204, changed.text
        elif change == "owner_ai_http":
            changed = await client.patch(url, json={"expected_revision": revision, "allow_owner_ai": False})
            assert changed.status_code == 200, changed.text
        elif change == "viewer":
            changed = await client.patch(url, json={"expected_revision": revision, "role": "viewer"})
            assert changed.status_code == 200, changed.text
        else:
            await _set_allow(app, world, False)
        return bound

    monkeypatch.setattr(collaboration_ai_guard.SendGuard, "prepare_provider", change_before_send)
    if entry == "preview":
        response = await editor.get(f"/api/v1/chapters/{chapter_id}/context-preview")
    else:
        response = await _search(editor, world, entry)
    assert response.status_code in (403, 404, 409), response.text
    assert "天剑宗位于北境" not in response.text and "掌门闭关" not in response.text
    assert fake_llm.embed_calls == []


# ---------------- 索引状态 ----------------


async def test_index_status_editor_view_hides_owner_config(app, client, make_client, fake_llm):
    editor = make_client()
    world = await _world(app, client, editor)
    # _world 的资料都在其他模型空间：按作者当前模型都待建向量。
    author = await client.get(f"{world['base']}/kb/index-status")
    assert author.status_code == 200, author.text
    author_body = author.json()
    assert author_body["embedding_model"] == "作者向量 · owner-emb"
    assert author_body["retrieval_mode"] == "vector" and author_body["owner_embedding_configured"] is True
    assert author_body["needs_rebuild"] is True and author_body["stale_count"] > 0
    shown = await editor.get(f"{world['base']}/kb/index-status")
    assert shown.status_code == 200, shown.text
    body = shown.json()
    for secret in ("作者向量", "owner-emb", "个人向量", "editor-emb", "config"):
        assert secret not in shown.text.replace("owner_embedding_configured", "")
    assert body["embedding_model"] is None
    assert body["owner_embedding_configured"] is True and body["retrieval_mode"] == "vector"
    assert body["keyword_reason"] is None
    # 待建向量数（裁决 7）：作者与编辑者同口径。
    assert (body["stale_count"], body["stale"], body["needs_rebuild"]) == (
        author_body["stale_count"], author_body["stale"], author_body["needs_rebuild"],
    )
    await _author_vectors(app, world["project"])
    assert (await editor.get(f"{world['base']}/kb/index-status")).json()["stale_count"] == 0
    assert fake_llm.embed_calls == []


@pytest.mark.parametrize(("case", "reason"), [
    ("unauthorized", "owner_embedding_not_allowed"), ("owner_unconfigured", "embedding_config_missing"),
])
async def test_index_status_editor_keyword_reasons(app, client, make_client, fake_llm, case, reason):
    editor = make_client()
    world = await _world(app, client, editor, allow_owner_ai=case != "unauthorized",
                         owner_embedding=case != "owner_unconfigured")
    body = (await editor.get(f"{world['base']}/kb/index-status")).json()
    assert body["retrieval_mode"] == "keyword" and body["keyword_reason"] == reason
    assert body["embedding_model"] is None
    assert body["owner_embedding_configured"] is (case != "owner_unconfigured")
    if case == "owner_unconfigured":
        author = (await client.get(f"{world['base']}/kb/index-status")).json()
        assert author["retrieval_mode"] == "keyword" and author["keyword_reason"] == reason
    assert fake_llm.embed_calls == []


async def _break_owner_embedding(app, world, how: str) -> None:
    """作者向量配置仍存在，但本地不可用（密钥无法解密 / 端点格式无效）。"""
    from app.models import AIConfig

    values = {"api_key_enc": "not-a-valid-token"} if how == "decrypt" else {"base_url": "ftp://bad-endpoint"}
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(
            AIConfig.user_id == world["author"]["id"], AIConfig.model_type == "embedding",
        ).values(**values))
        await db.commit()


@pytest.mark.parametrize("how", ["decrypt", "endpoint"])
@pytest.mark.parametrize("who", ["editor", "author"])
async def test_index_status_owner_embedding_unavailable(app, client, make_client, fake_llm, who, how):
    """裁决 16：作者配置存在但本地不可用 → keyword + owner_embedding_unavailable，作者与编辑者同口径；
    编辑者视图仍不暴露配置名 / 模型名 / 故障细节；检索确实只走关键词、零向量调用。"""
    editor = make_client()
    world = await _world(app, client, editor)
    await _memory(app, world["project"])
    await _author_vectors(app, world["project"])
    await _break_owner_embedding(app, world, how)
    guest = editor if who == "editor" else client
    response = await guest.get(f"{world['base']}/kb/index-status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["retrieval_mode"] == "keyword" and body["keyword_reason"] == "owner_embedding_unavailable"
    assert body["owner_embedding_configured"] is True
    for leaked in ("not-a-valid-token", "bad-endpoint", "Base URL"):
        assert leaked not in response.text
    if who == "editor":
        assert body["embedding_model"] is None
        for secret in ("作者向量", "owner-emb", "个人向量", "editor-emb"):
            assert secret not in response.text
    else:
        # 作者完整视图不变：仍显示本人配置名与模型名。
        assert body["embedding_model"] == "作者向量 · owner-emb"
    searched = await _search(guest, world, "kb")
    assert searched.status_code == 200, searched.text
    assert searched.json()["mode"] == "keyword"
    assert fake_llm.embed_calls == []


async def test_index_status_unauthorized_reason_wins_over_unavailable(app, client, make_client, fake_llm):
    """未授权编辑者即使作者配置不可用，也只报 owner_embedding_not_allowed（不探测作者配置状态）。"""
    editor = make_client()
    world = await _world(app, client, editor, allow_owner_ai=False)
    await _break_owner_embedding(app, world, "decrypt")
    body = (await editor.get(f"{world['base']}/kb/index-status")).json()
    assert body["retrieval_mode"] == "keyword" and body["keyword_reason"] == "owner_embedding_not_allowed"
    author = (await client.get(f"{world['base']}/kb/index-status")).json()
    assert author["keyword_reason"] == "owner_embedding_unavailable"
    assert fake_llm.embed_calls == []


async def test_index_status_unauthorized_editor_cannot_probe_owner_config_health(app, client, make_client, fake_llm):
    """未授权编辑者的 index-status 响应在作者配置可用 / 不可用两种情形下逐字一致（不能借响应差异探测作者配置状态）。"""
    editor = make_client()
    world = await _world(app, client, editor, allow_owner_ai=False)
    await _memory(app, world["project"])
    await _author_vectors(app, world["project"])
    healthy = await editor.get(f"{world['base']}/kb/index-status")
    assert healthy.status_code == 200, healthy.text
    assert (await client.get(f"{world['base']}/kb/index-status")).json()["retrieval_mode"] == "vector"
    for how in ("decrypt", "endpoint"):
        await _break_owner_embedding(app, world, how)
        broken = await editor.get(f"{world['base']}/kb/index-status")
        assert broken.status_code == 200, broken.text
        assert broken.json() == healthy.json()
    author = (await client.get(f"{world['base']}/kb/index-status")).json()
    assert author["keyword_reason"] == "owner_embedding_unavailable"
    assert fake_llm.embed_calls == []


async def test_index_status_counts_editor_keyword_rows_as_pending(app, client, make_client, fake_llm):
    """裁决 7 / E6-c 5b：未授权编辑者上传的关键词资料计入「待建向量」，作者视图据此提示补建。"""
    editor = make_client()
    world = await _world(app, client, editor, allow_owner_ai=False)
    await _author_vectors(app, world["project"])
    assert (await client.get(f"{world['base']}/kb/index-status")).json()["stale_count"] == 0
    uploaded = await editor.post(f"{world['base']}/kb/documents", files={"file": ("新.txt", "新资料天剑".encode())})
    assert uploaded.status_code == 202 and uploaded.json()["task"] is None
    author = (await client.get(f"{world['base']}/kb/index-status")).json()
    assert author["stale"]["kb"] >= 1 and author["needs_rebuild"] is True
    assert (await editor.get(f"{world['base']}/kb/index-status")).json()["stale_count"] == author["stale_count"]
    assert fake_llm.embed_calls == []


async def test_editor_personal_embedding_never_used_even_without_owner_config(app, client, make_client, fake_llm):
    """作者无向量配置、协作者本人有（且索引恰在协作者模型空间）：检索与预览仍只用关键词。"""
    editor = make_client()
    world = await _world(app, client, editor, owner_embedding=False)
    async with app.state.sessionmaker() as db:
        await db.execute(update(RetrievalChunk).where(RetrievalChunk.project_id == world["project"]).values(
            embedding_key="openai_compat:editor-emb:8",
        ))
        await db.commit()
    retrieval_service.invalidate(world["project"])
    chapter_id = await _chapter(client, world)
    assert (await _search(editor, world, "kb")).json()["mode"] == "keyword"
    assert (await editor.get(f"/api/v1/chapters/{chapter_id}/context-preview")).status_code == 200
    await create_ai_config(editor, name="另一个", model_type="embedding", model="editor-emb-2")
    assert (await _search(editor, world, "kb")).json()["mode"] == "keyword"
    assert fake_llm.embed_calls == []


# ---------------- 复核补充：普通故障降级与提示脱敏 ----------------


@pytest.mark.parametrize("endpoint", ["kb", "memory"])
async def test_editor_search_provider_error_warning_hides_owner_config(app, client, make_client, fake_llm, endpoint):
    """作者向量服务普通故障：编辑者退回关键词（裁决 11），提示不含提供方原文（可能带作者模型名 / 端点）。"""
    from app.ai.providers.base import AIProviderError

    editor = make_client()
    world = await _world(app, client, editor)
    await _memory(app, world["project"])
    await _author_vectors(app, world["project"])
    fake_llm.embed_error = AIProviderError("模型调用失败：model owner-emb at http://owner-secret.example not found")
    response = await _search(editor, world, endpoint)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "keyword" and body["hits"]
    assert body["warnings"] == ["查询向量生成失败，本次只用关键词检索"]
    for secret in ("owner-emb", "owner-secret", "作者向量"):
        assert secret not in response.text
    # 作者本人仍看到原始提示（行为不变）。
    fake_llm.embed_error = AIProviderError("模型调用失败：owner-secret")
    own = await client.post(f"{world['base']}/kb/search", json={"query": QUERY, "min_score": 0})
    assert own.status_code == 200 and "owner-secret" in own.text


@pytest.mark.parametrize("entry", ["kb", "preview"])
async def test_editor_prepare_failure_degrades_to_keyword_when_grant_unchanged(app, client, make_client, fake_llm,
                                                                               monkeypatch, entry):
    """作者端点构造失败（DNS 等普通故障，授权未变）：与作者本人一致退回关键词，零向量调用、不泄露端点。"""
    from app.core.errors import BadRequestError
    from app.services import ai_config_service

    editor = make_client()
    world = await _world(app, client, editor)
    await _author_vectors(app, world["project"])
    chapter_id = await _chapter(client, world)
    original = ai_config_service.build_provider

    async def failing(cfg, *args, **kwargs):
        if cfg.model_type == "embedding":
            raise BadRequestError("端点 owner-secret.example 无法解析")
        return await original(cfg, *args, **kwargs)

    monkeypatch.setattr(ai_config_service, "build_provider", failing)
    if entry == "preview":
        response = await editor.get(f"/api/v1/chapters/{chapter_id}/context-preview")
        assert response.status_code == 200, response.text
        assert "天剑宗位于北境雪山之巅" in response.json()["knowledge"]
    else:
        response = await _search(editor, world, "kb")
        assert response.status_code == 200, response.text
        assert response.json()["mode"] == "keyword" and response.json()["hits"]
    assert "owner-secret" not in response.text and "owner-emb" not in response.text
    assert fake_llm.embed_calls == []


@pytest.mark.parametrize("change", ["owner_ai_direct", "viewer", "revoke"])
async def test_editor_prepare_failure_with_grant_change_fails_closed(app, client, make_client, fake_llm, monkeypatch,
                                                                     change):
    """构造期间授权已变化：不能被当作普通故障降级为关键词结果，失败关闭且响应不含资料。"""
    editor = make_client()
    world = await _world(app, client, editor)
    await _author_vectors(app, world["project"])
    url = f"{world['base']}/collaborators/{world['actor']['id']}"
    original = collaboration_ai_guard.SendGuard.prepare_provider

    async def change_then_fail(guard, *args, **kwargs):
        async with app.state.sessionmaker() as db:
            revision = await db.scalar(select(ProjectCollaborator.revision).where(
                ProjectCollaborator.project_id == world["project"],
                ProjectCollaborator.user_id == world["actor"]["id"],
            ))
        if change == "revoke":
            assert (await client.delete(url, params={"expected_revision": revision})).status_code == 204
        elif change == "viewer":
            changed = await client.patch(url, json={"expected_revision": revision, "role": "viewer"})
            assert changed.status_code == 200, changed.text
        else:
            await _set_allow(app, world, False)
        return await original(guard, *args, **kwargs)

    monkeypatch.setattr(collaboration_ai_guard.SendGuard, "prepare_provider", change_then_fail)
    response = await _search(editor, world, "kb")
    assert response.status_code in (403, 404, 409), response.text
    assert "天剑宗位于北境" not in response.text
    assert fake_llm.embed_calls == []
