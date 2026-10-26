"""按建议重写（chapter.revise）：校验、版本与生成记录、与生成互斥、重写后自动重新分析与评分对比。"""

import pytest

from tests.helpers import Gate, analysis_json, create_ai_config, create_project, register, run_task, wait_task

ORIGINAL = "林远走进山门。\n\n守卫拦住了他。"


async def _setup(client, fake_llm):
    await register(client)
    await create_ai_config(client)
    p = await create_project(client, title="剑道", style="凌厉")
    base = f"/api/v1/projects/{p['id']}"
    ch = (await client.post(f"{base}/chapters", json={"title": "山门", "chapter_no": 1, "content": ORIGINAL})).json()
    fake_llm.push(analysis_json())
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/analyze"))
    assert task["status"] == "succeeded"
    analysis = (await client.get(f"/api/v1/chapters/{ch['id']}/analyses")).json()[0]
    return base, ch, analysis


def _revise(client, ch, **body):
    return client.post(f"/api/v1/chapters/{ch['id']}/revise", json=body)


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
async def test_revise_versions_logs_and_reanalysis(app, client, fake_llm):
    """AC7：勾选 2 条建议重写 → 覆盖正文，旧稿与新稿都在版本列表（新版本来源 revise），随后自动重新分析。"""
    base, ch, analysis = await _setup(client, fake_llm)
    url = f"{base}/chapters/{ch['id']}"
    fake_llm.push("重写后的正文，节奏更紧凑。")
    fake_llm.push(analysis_json(scores={"pacing": 9, "engagement": 9, "coherence": 8}))
    resp = await _revise(
        client,
        ch,
        analysis_id=analysis["id"],
        suggestion_ids=[1, 2],
        instruction="加强冲突",
        preserve=["保持人物性格", "保留关键对白"],
    )
    assert resp.status_code == 202, resp.text
    assert (resp.json()["kind"], resp.json()["title"]) == ("chapter.revise", "第 1 章 山门 · 按建议重写")
    # TaskOut.analysis_id：重写引用的分析（任务面板 / 其他标签页据此做评分对比）
    assert resp.json()["analysis_id"] == analysis["id"]
    task, events = await run_task(client, resp)
    assert task["status"] == "succeeded" and task["analysis_id"] == analysis["id"]
    assert events[-1][1]["result"]["chapter"]["content"] == "重写后的正文，节奏更紧凑。"
    assert "continue" not in [e for e, _ in events]

    prompt = fake_llm.calls[1]["messages"][1]["content"]
    assert "1. 开头铺垫过长，压缩到三段以内" in prompt and "2. 对手的台词更有个性" in prompt
    assert "补充试炼场的环境描写" not in prompt  # 未勾选的建议不进提示词
    assert "加强冲突" in prompt and "保持人物性格；保留关键对白" in prompt and ORIGINAL in prompt
    assert "目标字数：约 300 字" in prompt  # 未填目标字数：取原文字数（下限 300）

    versions = (await client.get(f"{url}/versions")).json()
    assert [v["source"] for v in versions][:2] == ["revise", "manual"]
    old = (await client.get(f"{url}/versions/{versions[1]['id']}")).json()
    assert old["content"] == ORIGINAL
    logs = (await client.get(f"{url}/generation-logs")).json()
    assert [(lg["kind"], lg["status"], lg["task_id"]) for lg in logs] == [("revise", "success", task["id"])]
    assert versions[0]["generation_log_id"] == logs[0]["id"]

    # 自动重新分析：完成后两次评分可对比
    tasks = (await client.get("/api/v1/tasks", params={"kind": "chapter.analyze", "chapter_id": ch["id"]})).json()
    assert len(tasks) == 2
    assert all(t["analysis_id"] is None for t in tasks)  # 只有 chapter.revise 有值
    listed = (await client.get("/api/v1/tasks", params={"kind": "chapter.revise"})).json()
    assert [t["analysis_id"] for t in listed] == [analysis["id"]]
    assert (await wait_task(client, tasks[0]["id"]))["status"] == "succeeded"
    history = (await client.get(f"/api/v1/chapters/{ch['id']}/analyses")).json()
    assert [h["scores"]["overall"] for h in history] == [8.7, 7.0]
    assert history[0]["stale"] is False and history[1]["stale"] is True


async def test_revise_validation(app, client, make_client, fake_llm):
    base, ch, analysis = await _setup(client, fake_llm)
    other = (await client.post(f"{base}/chapters", json={"title": "另一章", "content": "别的正文"})).json()
    empty = (await client.post(f"{base}/chapters", json={"title": "空章"})).json()
    cases = [
        (ch, {"analysis_id": analysis["id"]}, "请至少选择一条建议或填写修改要求"),
        (ch, {"analysis_id": analysis["id"], "suggestion_ids": [1, 9]}, "建议不存在：9"),
        (other, {"analysis_id": analysis["id"], "suggestion_ids": [1]}, "分析结果不存在或不属于该章节"),
        (empty, {"analysis_id": analysis["id"], "suggestion_ids": [1]}, "章节正文为空，无法重写"),
    ]
    for chapter, body, detail in cases:
        resp = await _revise(client, chapter, **body)
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == detail
    resp = await _revise(client, ch, analysis_id=analysis["id"], target_words=100, instruction="x")
    assert resp.status_code == 422
    # 校验失败不建任务、不抢占章节
    assert (await client.get(f"{base}/chapters/{ch['id']}")).json()["status"] == "draft"
    assert (await client.get("/api/v1/tasks", params={"kind": "chapter.revise"})).json() == []


async def test_revise_conflicts_with_generation(app, client, fake_llm):
    base, ch, analysis = await _setup(client, fake_llm)
    gate = Gate()
    fake_llm.push([gate, "生成中的正文"])
    gen = (await client.post(f"/api/v1/chapters/{ch['id']}/generate")).json()
    await gate.wait_reached()
    resp = await _revise(client, ch, analysis_id=analysis["id"], instruction="改")
    assert resp.status_code == 409 and resp.json()["detail"] == "该章节正在生成中，请稍候再试"
    gate.release()
    assert (await wait_task(client, gen["id"]))["status"] == "succeeded"

    # 重写失败：正文与版本不变，章节状态复位
    from app.ai.providers import AIProviderError

    fake_llm.push(["半截", AIProviderError("模型调用失败：额度不足")])
    task, _ = await run_task(client, await _revise(client, ch, analysis_id=analysis["id"], instruction="改"))
    assert task["status"] == "failed" and task["error"] == "模型调用失败：额度不足"
    saved = (await client.get(f"{base}/chapters/{ch['id']}")).json()
    assert saved["content"] == "生成中的正文" and saved["status"] == "done"
