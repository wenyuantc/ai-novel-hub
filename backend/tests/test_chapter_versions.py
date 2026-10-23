from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.ai.providers import AIProviderError
from app.models import Chapter, ChapterVersion, GenerationLog
from app.schemas.chapter import GenerateChapterRequest
from app.services import generation_log_service, generation_service, version_service
from tests.helpers import create_ai_config, create_project, parse_sse, register


async def _setup(client, *, ai: bool = True, content: str = ""):
    await register(client)
    p = await create_project(client)
    if ai:
        await create_ai_config(client)
    base = f"/api/v1/projects/{p['id']}"
    ch = (await client.post(f"{base}/chapters", json={"title": "第一章", "content": content})).json()
    return p, base, ch, f"{base}/chapters/{ch['id']}"


async def _versions(client, url):
    resp = await client.get(f"{url}/versions")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _shift_time(monkeypatch, minutes: int):
    real = datetime.now(UTC)
    monkeypatch.setattr(version_service, "_now", lambda: real + timedelta(minutes=minutes))


async def _set_content_raw(app, chapter_id: int, content: str) -> None:
    """绕过 service 直接改正文（模拟存量章节：有正文但没有版本）。"""
    async with app.state.sessionmaker() as db:
        row = await db.get(Chapter, chapter_id)
        row.content = content
        await db.commit()


# ---------- 手动保存 ----------


async def test_create_with_content_writes_manual_version(client):
    _, _, ch, url = await _setup(client, ai=False, content="开篇")
    vs = await _versions(client, url)
    assert [(v["seq"], v["source"], v["word_count"]) for v in vs] == [(1, "manual", 2)]
    assert "content" not in vs[0]
    detail = (await client.get(f"{url}/versions/{vs[0]['id']}")).json()
    assert detail["content"] == "开篇"


async def test_manual_save_merges_within_window(client, monkeypatch):
    _, _, _, url = await _setup(client, ai=False)
    assert await _versions(client, url) == []  # 空正文创建不写版本

    for text in ("一稿", "二稿", "三稿"):
        assert (await client.patch(url, json={"content": text})).status_code == 200
    vs = await _versions(client, url)
    assert len(vs) == 1 and vs[0]["source"] == "manual"
    assert (await client.get(f"{url}/versions/{vs[0]['id']}")).json()["content"] == "三稿"

    # 内容与最新版本相同：不写；清空：不写
    await client.patch(url, json={"content": "三稿"})
    await client.patch(url, json={"title": "改标题"})
    assert len(await _versions(client, url)) == 1

    # 超过 10 分钟：新建
    _shift_time(monkeypatch, 11)
    await client.patch(url, json={"content": "四稿"})
    vs = await _versions(client, url)
    assert [(v["seq"], v["source"]) for v in vs] == [(2, "manual"), (1, "manual")]

    await client.patch(url, json={"content": "   "})
    assert len(await _versions(client, url)) == 2


async def test_manual_merge_window_is_fixed_from_creation(app, client):
    """合并不延长窗口：按版本 created_at 计时，最近一次合并刷新的 updated_at 不参与判断。"""
    _, _, _, url = await _setup(client, ai=False, content="一稿")
    v1 = (await _versions(client, url))[0]
    now = datetime.now(UTC)
    async with app.state.sessionmaker() as db:
        row = await db.get(ChapterVersion, v1["id"])
        row.created_at = now - timedelta(minutes=11)
        row.updated_at = now - timedelta(minutes=1)
        await db.commit()
    await client.patch(url, json={"content": "二稿"})
    vs = await _versions(client, url)
    assert [(v["seq"], v["source"]) for v in vs] == [(2, "manual"), (1, "manual")]
    assert (await client.get(f"{url}/versions/{vs[1]['id']}")).json()["content"] == "一稿"


async def test_starred_manual_version_not_merged(client):
    _, _, _, url = await _setup(client, ai=False, content="原稿")
    v1 = (await _versions(client, url))[0]
    resp = await client.patch(f"{url}/versions/{v1['id']}", json={"starred": True, "note": " 定稿 "})
    assert resp.status_code == 200
    assert resp.json()["starred"] is True and resp.json()["note"] == "定稿"
    await client.patch(url, json={"content": "新稿"})
    vs = await _versions(client, url)
    assert [v["seq"] for v in vs] == [2, 1]


async def test_prune_keeps_starred_and_latest(app, client):
    _, _, ch, url = await _setup(client, ai=False)
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, ch["id"])
        chapter.content = "v1"
        first = await version_service.record(db, chapter, "manual")
        first.starred = True
        for i in range(2, 53):  # 再写 51 个未标星版本
            chapter.content = f"v{i}"
            await version_service.record(db, chapter, "manual")
        await db.commit()
        seqs = set(await db.scalars(select(ChapterVersion.seq).where(ChapterVersion.chapter_id == ch["id"])))
    assert seqs == {1} | set(range(3, 53))  # 最旧的未标星 v2 被删，标星 v1 保留

    vs = await _versions(client, url)
    assert vs[0]["seq"] == 52 and len(vs) == 51
    # 最新版本不能手动删除；其他可以
    resp = await client.delete(f"{url}/versions/{vs[0]['id']}")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "不能删除最新版本"
    assert (await client.delete(f"{url}/versions/{vs[1]['id']}")).status_code == 204
    assert (await client.get(f"{url}/versions/{vs[1]['id']}")).status_code == 404


# ---------- AI 生成 / 润色 ----------


async def test_generate_writes_baseline_and_generate_versions(app, client, fake_llm):
    _, _, ch, url = await _setup(client)
    await _set_content_raw(app, ch["id"], "旧稿")
    fake_llm.push("新的正文内容")
    events = parse_sse((await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 300})).text)
    assert events[-1][0] == "done"
    assert "warning" not in events[-1][1]

    vs = await _versions(client, url)
    assert [(v["seq"], v["source"]) for v in vs] == [(2, "generate"), (1, "baseline")]
    assert (await client.get(f"{url}/versions/{vs[1]['id']}")).json()["content"] == "旧稿"
    assert (await client.get(f"{url}/versions/{vs[0]['id']}")).json()["content"] == "新的正文内容"

    logs = (await client.get(f"{url}/generation-logs")).json()
    assert len(logs) == 1
    log = logs[0]
    assert vs[0]["generation_log_id"] == log["id"]
    assert (log["kind"], log["status"], log["model"], log["provider"]) == (
        "generate",
        "success",
        "gpt-test",
        "openai_compat",
    )
    assert log["output_chars"] == 6 and log["duration_ms"] >= 0
    assert log["params"]["target_words"] == 300 and log["params"]["continuation_rounds"] == 0
    assert log["prompt_tokens"] is None and "messages" not in log
    detail = (await client.get(f"{url}/generation-logs/{log['id']}")).json()
    assert detail["messages"] == fake_llm.calls[-1]["messages"]


async def test_generate_skips_baseline_when_saved(client, fake_llm):
    _, _, ch, url = await _setup(client, content="已保存的旧稿")
    fake_llm.push("润色后")
    events = parse_sse((await client.post(f"/api/v1/chapters/{ch['id']}/polish")).text)
    assert events[-1][0] == "done"
    vs = await _versions(client, url)
    assert [v["source"] for v in vs] == ["polish", "manual"]
    logs = (await client.get(f"{url}/generation-logs")).json()
    assert logs[0]["kind"] == "polish" and "continuation_rounds" not in logs[0]["params"]


async def test_generate_error_keeps_versions_and_logs_error(client, fake_llm):
    _, _, ch, url = await _setup(client, content="旧稿")
    fake_llm.push(["部分", AIProviderError("模型调用失败：额度不足")])
    events = parse_sse((await client.post(f"/api/v1/chapters/{ch['id']}/generate")).text)
    assert events[-1] == ("error", {"detail": "模型调用失败：额度不足"})

    assert [v["source"] for v in await _versions(client, url)] == ["manual"]
    assert (await client.get(url)).json()["content"] == "旧稿"
    logs = (await client.get(f"{url}/generation-logs")).json()
    assert [(lg["status"], lg["error"], lg["output_chars"]) for lg in logs] == [("error", "模型调用失败：额度不足", 2)]


async def test_generate_cancelled_logs_cancelled(app, client, fake_llm):
    _, _, ch, url = await _setup(client, content="旧稿")
    me = (await client.get("/api/v1/auth/me")).json()
    fake_llm.push(["第一块", "第二块"])
    async with app.state.sessionmaker() as db:
        job = await generation_service.prepare_chapter_job(
            db,
            user_id=me["id"],
            chapter_id=ch["id"],
            mode="generate",
            req=GenerateChapterRequest(),
            cipher=app.state.cipher,
            factory=app.state.provider_factory,
            ai_timeout=30,
            allow_private=False,
            context_budget=12000,
        )
    gen = generation_service.run_chapter_job(job, app.state.sessionmaker)
    assert await gen.__anext__() == ("delta", {"text": "第一块"})
    await gen.aclose()  # 模拟客户端断开

    saved = (await client.get(url)).json()
    assert saved["status"] == "draft" and saved["content"] == "旧稿"
    assert [v["source"] for v in await _versions(client, url)] == ["manual"]
    logs = (await client.get(f"{url}/generation-logs")).json()
    assert [lg["status"] for lg in logs] == ["cancelled"]


# ---------- 自动续写 ----------


async def test_auto_continue_rounds_and_join(client, fake_llm):
    _, _, ch, url = await _setup(client)
    fake_llm.push("甲" * 400)
    fake_llm.push("乙" * 300)
    fake_llm.push("丙" * 300)
    resp = await client.post(
        f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 1000, "auto_continue": True}
    )
    events = parse_sse(resp.text)
    assert [d for e, d in events if e == "continue"] == [
        {"round": 1, "words": 400, "target": 1000},
        {"round": 2, "words": 700, "target": 1000},
    ]
    expected = "甲" * 400 + "\n\n" + "乙" * 300 + "\n\n" + "丙" * 300
    assert events[-1][0] == "done"
    assert events[-1][1]["chapter"]["content"] == expected
    assert "".join(d["text"] for e, d in events if e == "delta") == expected
    assert len(fake_llm.calls) == 3
    last = fake_llm.calls[-1]["messages"]
    assert last[-2] == {"role": "assistant", "content": "甲" * 400 + "\n\n" + "乙" * 300}
    assert last[-1]["role"] == "user" and "继续" in last[-1]["content"]

    log = (await client.get(f"{url}/generation-logs")).json()[0]
    assert log["params"]["continuation_rounds"] == 2 and log["output_chars"] == 1000
    detail = (await client.get(f"{url}/generation-logs/{log['id']}")).json()
    # 记录中不重复存已生成正文
    assert all("甲" * 400 not in m["content"] for m in detail["messages"])


async def test_auto_continue_capped_at_two_rounds(client, fake_llm):
    _, _, ch, _ = await _setup(client)
    for _ in range(4):
        fake_llm.push("短" * 100)
    resp = await client.post(
        f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 3000, "auto_continue": True}
    )
    events = parse_sse(resp.text)
    assert len([e for e, _ in events if e == "continue"]) == 2
    assert len(fake_llm.calls) == 3
    assert events[-1][1]["chapter"]["word_count"] == 300


async def test_auto_continue_disabled(client, fake_llm):
    _, _, ch, _ = await _setup(client)
    fake_llm.push("甲" * 100)
    resp = await client.post(
        f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 3000, "auto_continue": False}
    )
    events = parse_sse(resp.text)
    assert not [e for e, _ in events if e == "continue"]
    assert len(fake_llm.calls) == 1
    # 接口默认不续写（兼容旧客户端）
    fake_llm.push("乙" * 100)
    events = parse_sse((await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={})).text)
    assert not [e for e, _ in events if e == "continue"] and len(fake_llm.calls) == 2


async def test_auto_continue_error_keeps_text_with_warning(client, fake_llm):
    _, _, ch, url = await _setup(client)
    fake_llm.push("甲" * 400)
    fake_llm.push(["半截", AIProviderError("模型调用失败：额度不足")])
    resp = await client.post(
        f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 1000, "auto_continue": True}
    )
    events = parse_sse(resp.text)
    assert events[-1][0] == "done"
    assert events[-1][1]["warning"] == "自动续写失败：模型调用失败：额度不足"
    assert events[-1][1]["chapter"]["content"] == "甲" * 400
    saved = (await client.get(url)).json()
    assert saved["content"] == "甲" * 400 and saved["status"] == "done"
    log = (await client.get(f"{url}/generation-logs")).json()[0]
    assert log["status"] == "success" and log["params"]["continuation_rounds"] == 1
    assert "自动续写失败" in log["error"]


# ---------- 回滚 ----------


async def test_restore_and_undo(app, client, monkeypatch):
    _, _, ch, url = await _setup(client, ai=False, content="A稿")
    _shift_time(monkeypatch, 11)
    await client.patch(url, json={"content": "B稿"})
    vs = await _versions(client, url)
    v1, v2 = vs[1], vs[0]

    resp = await client.post(f"{url}/versions/{v1['id']}/restore")
    assert resp.status_code == 200
    assert resp.json()["content"] == "A稿" and resp.json()["word_count"] == 2
    vs = await _versions(client, url)
    assert (vs[0]["seq"], vs[0]["source"], vs[0]["note"]) == (3, "restore", "恢复自 v1")

    # 撤销回滚：恢复回滚前的快照 v2
    resp = await client.post(f"{url}/versions/{v2['id']}/restore")
    assert resp.json()["content"] == "B稿"

    # 当前正文未被版本保存过：回滚前先补 baseline
    await _set_content_raw(app, ch["id"], "未存的C稿")
    await client.post(f"{url}/versions/{v1['id']}/restore")
    vs = await _versions(client, url)
    assert [v["source"] for v in vs[:2]] == ["restore", "baseline"]
    assert (await client.get(f"{url}/versions/{vs[1]['id']}")).json()["content"] == "未存的C稿"

    # 生成中不允许回滚
    async with app.state.sessionmaker() as db:
        row = await db.get(Chapter, ch["id"])
        row.status = "generating"
        await db.commit()
    resp = await client.post(f"{url}/versions/{v2['id']}/restore")
    assert resp.status_code == 409


async def test_log_prune_per_project(app, client):
    p, _, ch, _ = await _setup(client, ai=False)
    async with app.state.sessionmaker() as db:
        for _ in range(generation_log_service.MAX_LOGS_PER_PROJECT + 3):
            await generation_log_service.write_log(
                db,
                project_id=p["id"],
                chapter_id=ch["id"],
                kind="generate",
                ai_config_id=None,
                provider="openai_compat",
                model="m",
                params={},
                messages=[],
                status="success",
            )
        await db.commit()
        count = await db.scalar(select(func.count(GenerationLog.id)).where(GenerationLog.project_id == p["id"]))
        assert count == generation_log_service.MAX_LOGS_PER_PROJECT
        oldest = await db.scalar(select(func.min(GenerationLog.id)))
        assert oldest == 4


# ---------- 越权 ----------


async def test_versions_and_logs_cross_user_404(client, make_client, fake_llm):
    _, _, ch, url = await _setup(client, content="甲的正文")
    fake_llm.push("改写")
    await client.post(f"/api/v1/chapters/{ch['id']}/generate")
    vid = (await _versions(client, url))[0]["id"]
    lid = (await client.get(f"{url}/generation-logs")).json()[0]["id"]

    other = make_client()
    await register(other, "bob")
    await create_ai_config(other)
    checks = [
        ("GET", f"{url}/versions", None),
        ("GET", f"{url}/versions/{vid}", None),
        ("PATCH", f"{url}/versions/{vid}", {"starred": True}),
        ("DELETE", f"{url}/versions/{vid}", None),
        ("POST", f"{url}/versions/{vid}/restore", None),
        ("GET", f"{url}/generation-logs", None),
        ("GET", f"{url}/generation-logs/{lid}", None),
        ("POST", f"/api/v1/chapters/{ch['id']}/rewrite", {"op": "rewrite", "start": 0, "end": 2}),
        ("POST", f"{url}/rewrite/apply", {"start": 0, "end": 2, "original": "甲的", "replacement": "乙"}),
    ]
    for method, path, body in checks:
        resp = await other.request(method, path, json=body)
        assert resp.status_code == 404, (method, path, resp.text)
    assert fake_llm.calls and len(fake_llm.calls) == 1  # 越权的 rewrite 未调用模型
