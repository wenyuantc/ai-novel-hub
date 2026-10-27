"""剧情记忆：从分析抽取、重新分析替换并保留标记、删章清理、手动记忆、召回范围 / 钉住 / 屏蔽 / 去重、注入位置与预算、
检索出错降级、记忆向量任务、大纲续写注入、注入预览。"""

import pytest
from sqlalchemy import func, select

from app.models import RetrievalChunk, StoryMemory
from app.services import memory_service, retrieval_service
from tests.helpers import analysis_json, create_ai_config, create_project, register, run_task, wait_task

MEM_HEADER = "【相关旧情节】"
KB_HEADER = "【参考资料（来自知识库，仅供参考；与项目设定冲突时以设定为准）】"


@pytest.fixture(autouse=True)
def _clear_cache():
    retrieval_service.invalidate()
    yield
    retrieval_service.invalidate()


async def _setup(client, n=1):
    await register(client)
    await create_ai_config(client)
    p = await create_project(client, title="剑道", genre="仙侠")
    base = f"/api/v1/projects/{p['id']}"
    await client.post(f"{base}/characters", json={"name": "林远", "role_type": "主角"})
    chs = []
    for i in range(1, n + 1):
        resp = await client.post(
            f"{base}/chapters", json={"title": f"第{i}章", "chapter_no": i, "content": f"第{i}章的正文。林远继续前行。"}
        )
        chs.append(resp.json())
    return p, base, chs


async def _analyze(client, fake_llm, ch, **overrides) -> dict:
    fake_llm.push(analysis_json(**overrides))
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/analyze"))
    assert task["status"] == "succeeded", task
    return task


async def _memories(client, base, **params) -> list[dict]:
    return (await client.get(f"{base}/memories", params=params)).json()["items"]


def _quiet(**kw) -> dict:
    """只保留指定内容的分析输出（其他类别清空），便于断言召回。"""
    base = {
        "summary": "",
        "plot_points": [],
        "hooks": [],
        "character_changes": [],
        "foreshadows": [],
    }
    base.update(kw)
    return base


async def _generate(client, fake_llm, ch) -> str:
    fake_llm.push("新的正文")
    task, _ = await run_task(
        client, await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 500})
    )
    assert task["status"] == "succeeded", task
    return fake_llm.calls[-1]["messages"][-1]["content"]


# ---------------- 抽取 ----------------


async def test_memories_from_analysis(client, fake_llm, app):
    _, base, (ch,) = await _setup(client)
    await _analyze(
        client,
        fake_llm,
        ch,
        plot_points=[
            {"text": "山门试炼开始", "importance": 3},
            {"text": "林远击败对手", "importance": 5},
            {"text": "无关紧要的小事", "importance": 2},
        ],
        hooks=[
            {"type": "悬念", "strength": 8, "text": "神秘人现身"},
            {"type": "其他", "strength": 5, "text": "弱钩子"},
        ],
        character_changes=[
            {"name": "林远", "current_state": "身受重伤"},
            {"name": "赵六", "life_status": "dead"},
        ],
        foreshadows=[
            {"action": "plant", "text": "神秘玉佩", "keyword": "玉佩"},
            {"action": "resolve", "text": "旧日誓约", "keyword": "誓约"},
        ],
    )
    items = await _memories(client, base)
    got = {(m["kind"], m["content"], m["importance"]) for m in items}
    assert got == {
        ("summary", "林远在山门试炼中击败对手，却身受重伤。", 3),
        ("plot_point", "山门试炼开始", 3),
        ("plot_point", "林远击败对手", 5),
        ("hook", "神秘人现身", 4),
        ("character", "林远：身受重伤", 3),
        ("character", "赵六：已死亡", 5),
        ("foreshadow", "埋下：神秘玉佩", 4),
        ("foreshadow", "回收：旧日誓约", 4),
    }
    assert all(m["chapter_no"] == 1 and m["chapter_id"] == ch["id"] and m["origin"] == "auto" for m in items)
    by_content = {m["content"]: m for m in items}
    assert by_content["林远击败对手"]["characters"] == ["林远"]
    assert by_content["赵六：已死亡"]["characters"] == ["赵六"]
    assert by_content["神秘人现身"]["characters"] == []
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(RetrievalChunk).where(RetrievalChunk.source == "memory")))
    assert {r.ref_id for r in rows} == {m["id"] for m in items}
    assert all(r.chapter_no == 1 and r.enabled for r in rows)
    # 分析即可检索（关键词）
    res = (await client.post(f"{base}/memories/search", json={"query": "玉佩", "min_score": 0})).json()
    assert [h["memory"]["content"] for h in res["hits"]] == ["埋下：神秘玉佩"]


def test_content_key_normalization():
    assert memory_service.content_key("plot_point", "林远 击败对手。") == memory_service.content_key(
        "plot_point", "林远击败对手！"
    )
    assert memory_service.content_key("plot_point", "ABC") == memory_service.content_key("plot_point", "abc")
    assert memory_service.content_key("hook", "x") != memory_service.content_key("plot_point", "x")


async def test_reanalysis_replaces_and_keeps_flags(client, fake_llm, app):
    _, base, (ch,) = await _setup(client)
    await _analyze(client, fake_llm, ch)
    items = {m["content"]: m for m in await _memories(client, base)}
    pinned_id = items["林远击败对手"]["id"]
    muted_id = items["神秘人现身"]["id"]
    assert (await client.patch(f"{base}/memories/{pinned_id}", json={"pinned": True})).json()["pinned"] is True
    assert (await client.patch(f"{base}/memories/{muted_id}", json={"muted": True})).json()["muted"] is True
    manual = (
        await client.post(f"{base}/memories", json={"chapter_no": 1, "content": "林家祖传玉佩", "characters": ["林远"]})
    ).json()

    # 重新分析：同类型同内容（标点 / 空白不同）继承标记，新内容新建，旧的删除，手动记忆不变
    await _analyze(
        client,
        fake_llm,
        ch,
        plot_points=[{"text": "林远 击败对手！", "importance": 5}, {"text": "新的转折", "importance": 4}],
        hooks=[{"type": "悬念", "strength": 9, "text": "神秘人现身"}],
        foreshadows=[],
    )
    items = {m["content"]: m for m in await _memories(client, base)}
    assert "山门试炼开始" not in items and "埋下：神秘玉佩" not in items
    assert items["林远 击败对手！"]["pinned"] is True and items["林远 击败对手！"]["id"] == pinned_id
    assert items["神秘人现身"]["muted"] is True and items["神秘人现身"]["importance"] == 5
    assert items["新的转折"]["pinned"] is False
    assert items["林家祖传玉佩"]["id"] == manual["id"] and items["林家祖传玉佩"]["origin"] == "manual"
    async with app.state.sessionmaker() as db:
        rows = {r.ref_id: r for r in await db.scalars(select(RetrievalChunk).where(RetrievalChunk.source == "memory"))}
    assert set(rows) == {m["id"] for m in items.values()}
    assert rows[muted_id].enabled is False and rows[pinned_id].pinned is True
    assert "击败对手！" in rows[pinned_id].text
    # 屏蔽的不会被检索到
    res = (await client.post(f"{base}/memories/search", json={"query": "神秘人", "min_score": 0})).json()
    assert res["hits"] == []
    res = (
        await client.post(f"{base}/memories/search", json={"query": "神秘人", "min_score": 0, "include_muted": True})
    ).json()
    assert [h["memory"]["id"] for h in res["hits"]] == [muted_id]


async def test_chapter_delete_removes_auto_memories(client, fake_llm, app):
    _, base, (ch,) = await _setup(client)
    await _analyze(client, fake_llm, ch)
    manual = (await client.post(f"{base}/memories", json={"chapter_no": 1, "content": "手动备忘"})).json()
    assert manual["chapter_id"] == ch["id"]
    assert (await client.delete(f"{base}/chapters/{ch['id']}")).status_code == 204
    items = await _memories(client, base)
    assert [(m["id"], m["chapter_id"], m["chapter_no"]) for m in items] == [(manual["id"], None, 1)]
    async with app.state.sessionmaker() as db:
        refs = list(await db.scalars(select(RetrievalChunk.ref_id).where(RetrievalChunk.source == "memory")))
    assert refs == [manual["id"]]


async def test_chapter_renumber_moves_memories(client, fake_llm, app):
    _, base, (ch,) = await _setup(client)
    await _analyze(client, fake_llm, ch)
    await client.patch(f"{base}/chapters/{ch['id']}", json={"chapter_no": 7})
    assert {m["chapter_no"] for m in await _memories(client, base)} == {7}
    async with app.state.sessionmaker() as db:
        nos = set(await db.scalars(select(RetrievalChunk.chapter_no).where(RetrievalChunk.source == "memory")))
    assert nos == {7}


# ---------------- 手动管理 ----------------


async def test_manual_memory_api(client, fake_llm):
    _, base, (ch,) = await _setup(client)
    await _analyze(client, fake_llm, ch)
    auto = (await _memories(client, base, kind="summary"))[0]

    resp = await client.post(f"{base}/memories", json={"chapter_no": 3, "content": "  师父留下遗言  ", "importance": 5})
    assert resp.status_code == 201
    m = resp.json()
    assert m["content"] == "师父留下遗言" and m["kind"] == "manual" and m["chapter_id"] is None
    assert (await client.post(f"{base}/memories", json={"chapter_no": 1, "content": "   "})).status_code == 422
    assert (await client.post(f"{base}/memories", json={"chapter_no": 0, "content": "x"})).status_code == 422

    resp = await client.patch(f"{base}/memories/{m['id']}", json={"content": "师父的遗言", "chapter_no": 1})
    assert resp.status_code == 200 and resp.json()["content"] == "师父的遗言"
    assert resp.json()["chapter_id"] == ch["id"]
    resp = await client.patch(f"{base}/memories/{auto['id']}", json={"content": "改掉"})
    assert resp.status_code == 400 and resp.json()["detail"] == "自动记忆只能钉住或屏蔽，不能修改内容"
    resp = await client.delete(f"{base}/memories/{auto['id']}")
    assert resp.status_code == 400 and resp.json()["detail"] == "自动记忆不能删除，可以屏蔽它"

    # 筛选
    assert [x["id"] for x in await _memories(client, base, origin="manual")] == [m["id"]]
    assert [x["kind"] for x in await _memories(client, base, kind="hook")] == ["hook"]
    assert [x["id"] for x in await _memories(client, base, q="遗言")] == [m["id"]]
    await client.patch(f"{base}/memories/{auto['id']}", json={"pinned": True})
    assert [x["id"] for x in await _memories(client, base, pinned=True)] == [auto["id"]]
    page = (await client.get(f"{base}/memories", params={"page_size": 2})).json()
    assert page["total"] == len(await _memories(client, base)) and len(page["items"]) == 2
    assert await _memories(client, base, chapter_from=2) == []

    # 检索调试：按第 N 章召回
    res = (await client.post(f"{base}/memories/search", json={"query": "师父 遗言", "min_score": 0})).json()
    assert res["hits"][0]["memory"]["id"] == m["id"] and res["mode"] == "keyword"
    res = (
        await client.post(
            f"{base}/memories/search", json={"query": "师父 遗言", "before_chapter_no": 1, "min_score": 0}
        )
    ).json()
    assert res["hits"] == []

    assert (await client.delete(f"{base}/memories/{m['id']}")).status_code == 204
    assert (await client.delete(f"{base}/memories/{m['id']}")).status_code == 404


# ---------------- 召回与注入 ----------------


async def test_recall_scope_pin_mute_and_position(client, fake_llm):
    _, base, chs = await _setup(client, n=5)
    await _analyze(
        client,
        fake_llm,
        chs[0],
        **_quiet(summary="第一章摘要：玉佩现世", plot_points=[{"text": "玉佩发光", "importance": 4}]),
    )
    await _analyze(
        client,
        fake_llm,
        chs[1],
        **_quiet(summary="第二章摘要", hooks=[{"type": "悬念", "strength": 7, "text": "龙宫夜宴的请柬"}]),
    )
    await _analyze(client, fake_llm, chs[2], **_quiet(plot_points=[{"text": "玉佩碎裂", "importance": 5}]))
    await _analyze(client, fake_llm, chs[3], **_quiet(plot_points=[{"text": "玉佩重铸", "importance": 5}]))
    manual = (await client.post(f"{base}/memories", json={"chapter_no": 2, "content": "玉佩是林家祖传之物"})).json()
    items = {m["content"]: m for m in await _memories(client, base)}
    # 钉住一条与查询无关的钩子
    await client.patch(f"{base}/memories/{items['龙宫夜宴的请柬']['id']}", json={"pinned": True})

    await client.patch(f"{base}/chapters/{chs[4]['id']}", json={"title": "玉佩之谜"})
    prompt = await _generate(client, fake_llm, chs[4])
    assert MEM_HEADER in prompt
    block = prompt.split(MEM_HEADER + "\n", 1)[1].split("\n\n", 1)[0].splitlines()
    # 只召回前情没有全文的章节（第 3、4 章在最近章节全文中）；第 1、2 章的摘要已在前情提要中，不重复
    assert block == [
        "- 第1章（情节）：玉佩发光",
        "- 第2章（钩子）：龙宫夜宴的请柬",
        "- 第2章（备忘）：玉佩是林家祖传之物",
    ]
    # 位置：前情提要之后、最近章节正文之前
    assert prompt.index("【前情提要（更早章节）】") < prompt.index(MEM_HEADER) < prompt.index("【最近章节正文】")
    assert "第一章摘要：玉佩现世" in prompt.split(MEM_HEADER)[0]

    # 屏蔽后不再召回；钉住的即使不相关也注入
    await client.patch(f"{base}/memories/{items['玉佩发光']['id']}", json={"muted": True})
    prompt = await _generate(client, fake_llm, chs[4])
    block = prompt.split(MEM_HEADER + "\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert block == ["- 第2章（钩子）：龙宫夜宴的请柬", "- 第2章（备忘）：玉佩是林家祖传之物"]
    assert manual["id"]

    # 生成第 2 章：只召回第 2 章之前的（第 1 章正文在最近章节中 → 没有可召回的），不出现空块
    prompt = await _generate(client, fake_llm, chs[1])
    assert MEM_HEADER not in prompt


async def test_recall_checks_muted_on_memory_rows(client, fake_llm, app):
    """召回以记忆表的 muted 为准：即使检索单元的 enabled 没同步（数据漂移），屏蔽的记忆也不注入。"""
    from app.models import Project

    _, base, chs = await _setup(client, n=4)
    await _analyze(client, fake_llm, chs[0], **_quiet(plot_points=[{"text": "玉佩发光", "importance": 4}]))
    target = (await _memories(client, base))[0]
    async with app.state.sessionmaker() as db:
        m = await db.get(StoryMemory, target["id"])
        m.muted = True  # 只改记忆表，不经 update_memory（检索单元仍 enabled）
        await db.commit()
        project = await db.get(Project, m.project_id)
        recalled = await memory_service.recall(db, project, 4, "玉佩发光", embedder=None)
    assert recalled == []


async def test_pinned_first_and_budget(client, fake_llm):
    _, base, chs = await _setup(client, n=5)
    long_points = [{"text": f"玉佩线索{i}：" + "细节" * 40, "importance": 4} for i in range(5)]
    await _analyze(client, fake_llm, chs[0], **_quiet(plot_points=long_points))
    await _analyze(client, fake_llm, chs[1], **_quiet(plot_points=[{"text": "无关的钉住情节", "importance": 3}]))
    pinned = next(m for m in await _memories(client, base) if m["content"] == "无关的钉住情节")
    await client.patch(f"{base}/memories/{pinned['id']}", json={"pinned": True})
    await client.patch(
        f"/api/v1/projects/{chs[0]['project_id']}/kb/settings", json={"memory_max_chars": 300, "memory_min_score": 0}
    )
    await client.patch(f"{base}/chapters/{chs[4]['id']}", json={"title": "玉佩线索"})
    prompt = await _generate(client, fake_llm, chs[4])
    block = prompt.split(MEM_HEADER + "\n", 1)[1].split("\n\n", 1)[0]
    lines = block.splitlines()
    assert "- 第2章（情节）：无关的钉住情节" in lines
    assert len(block) <= 300 and len(lines) < 6
    # 按章号排列
    assert lines == sorted(lines, key=lambda x: int(x.split("第")[1].split("章")[0]))


async def test_memory_and_kb_separate_blocks(client, fake_llm):
    p, base, chs = await _setup(client, n=4)
    await _analyze(client, fake_llm, chs[0], **_quiet(plot_points=[{"text": "天剑宗收徒", "importance": 4}]))
    resp = await client.post(
        f"{base}/kb/documents",
        files={"file": ("设定.md", "# 门派\n\n天剑宗位于北境雪山。".encode(), "text/markdown")},
    )
    await wait_task(client, resp.json()["task"]["id"])
    await client.patch(f"{base}/chapters/{chs[3]['id']}", json={"title": "天剑宗"})
    prompt = await _generate(client, fake_llm, chs[3])
    assert KB_HEADER in prompt and MEM_HEADER in prompt
    kb_block = prompt.split(KB_HEADER + "\n", 1)[1].split("\n\n", 1)[0]
    mem_block = prompt.split(MEM_HEADER + "\n", 1)[1].split("\n\n", 1)[0]
    assert kb_block == "- 《设定.md》门派：天剑宗位于北境雪山。"
    assert mem_block == "- 第1章（情节）：天剑宗收徒"

    # 注入预览与实际一致
    preview = (await client.get(f"/api/v1/chapters/{chs[3]['id']}/context-preview")).json()
    assert preview == {"knowledge": f"{KB_HEADER}\n{kb_block}", "memories": f"{MEM_HEADER}\n{mem_block}"}
    preview = (await client.get(f"/api/v1/chapters/{chs[0]['id']}/context-preview")).json()
    assert preview["memories"] == ""


async def test_retrieval_error_does_not_block_generation(client, fake_llm, monkeypatch):
    _, base, chs = await _setup(client, n=4)
    await _analyze(client, fake_llm, chs[0], **_quiet(plot_points=[{"text": "天剑宗收徒", "importance": 4}]))
    pin = (await _memories(client, base))[0]
    await client.patch(f"{base}/memories/{pin['id']}", json={"pinned": True})
    await client.post(
        f"{base}/kb/documents", files={"file": ("设定.md", "# 门派\n\n天剑宗位于北境。".encode(), "text/markdown")}
    )

    async def broken(*args, **kwargs):
        raise RuntimeError("索引损坏")

    monkeypatch.setattr(retrieval_service, "search", broken)
    await client.patch(f"{base}/chapters/{chs[3]['id']}", json={"title": "天剑宗"})
    prompt = await _generate(client, fake_llm, chs[3])
    assert "参考资料（来自知识库" not in prompt
    # 记忆召回出错时整块不注入（包括钉住的），不出现空块
    assert MEM_HEADER not in prompt


async def test_memory_vectors_task(client, fake_llm, app):
    _, base, (ch,) = await _setup(client)
    await create_ai_config(client, name="向量", model_type="embedding", model="emb")
    await _analyze(client, fake_llm, ch)
    async with app.state.sessionmaker() as db:
        from app.models import Task

        tasks = list(await db.scalars(select(Task).where(Task.kind == "kb.index")))
    assert len(tasks) == 1 and tasks[0].title == "剧情记忆向量"
    assert (await wait_task(client, tasks[0].id))["status"] == "succeeded"
    async with app.state.sessionmaker() as db:
        missing = await db.scalar(
            select(func.count(RetrievalChunk.id)).where(
                RetrievalChunk.source == "memory", RetrievalChunk.embedding.is_(None)
            )
        )
        total = await db.scalar(select(func.count(StoryMemory.id)))
    assert missing == 0 and total > 0
    res = (await client.post(f"{base}/memories/search", json={"query": "玉佩", "min_score": 0})).json()
    assert res["mode"] == "hybrid" and res["hits"][0]["vector_score"] is not None
    # 手动记忆：同样排队生成向量
    await client.post(f"{base}/memories", json={"chapter_no": 1, "content": "新的手动记忆"})
    status = await _wait_no_stale(client, base)
    assert status["stale"]["memory"] == 0


async def _wait_no_stale(client, base):
    import asyncio

    for _ in range(200):
        status = (await client.get(f"{base}/kb/index-status")).json()
        if status["stale_count"] == 0 and not status["indexing"]:
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(status)


async def test_outline_continue_injects_memories(client, fake_llm):
    p, base, chs = await _setup(client, n=2)
    await _analyze(
        client, fake_llm, chs[0], **_quiet(summary="林远入门", plot_points=[{"text": "玉佩失窃", "importance": 4}])
    )
    await client.post(f"{base}/outlines", json={"title": "玉佩风波", "chapter_no": 1})
    fake_llm.push('{"chapters": []}')
    await run_task(client, await client.post(f"{base}/outlines/continue", json={"count": 2, "direction": "追查玉佩"}))
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "【前文进展（每章摘要）】\n第1章 第1章：林远入门" in prompt
    block = prompt.split(MEM_HEADER + "\n", 1)[1].split("\n\n", 1)[0]
    # 摘要已在「前文进展」中，不重复召回
    assert block == "- 第1章（情节）：玉佩失窃"


async def test_cross_user_memories_404(client, make_client, fake_llm):
    _, base, (ch,) = await _setup(client)
    m = (await client.post(f"{base}/memories", json={"chapter_no": 1, "content": "备忘"})).json()
    bob = make_client()
    await register(bob, "bob")
    for method, url, body in (
        ("GET", f"{base}/memories", None),
        ("POST", f"{base}/memories", {"chapter_no": 1, "content": "x"}),
        ("POST", f"{base}/memories/search", {"query": "x"}),
        ("PATCH", f"{base}/memories/{m['id']}", {"pinned": True}),
        ("DELETE", f"{base}/memories/{m['id']}", None),
        ("GET", f"/api/v1/chapters/{ch['id']}/context-preview", None),
    ):
        resp = await bob.request(method, url, json=body)
        assert resp.status_code == 404, (method, url, resp.text)
    assert (await _memories(client, base))[0]["pinned"] is False


async def test_failed_analysis_writes_no_memories(client, fake_llm, app):
    _, base, (ch,) = await _setup(client)
    fake_llm.push("这不是 JSON")
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/analyze"))
    assert task["status"] == "failed"
    assert await _memories(client, base) == []
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(RetrievalChunk.id))) == 0
