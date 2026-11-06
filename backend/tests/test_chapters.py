from sqlalchemy import select

from app.ai.providers import AIProviderError
from app.models import Chapter
from app.services.chapter_service import count_words
from tests.helpers import (
    Gate,
    create_ai_config,
    create_project,
    final_event,
    parse_sse,
    register,
    run_task,
    stream_with_gate,
)


def test_count_words():
    assert count_words("你好，世界！\n\n  第二段 abc ") == 12
    assert count_words("") == 0


async def _setup(client):
    await register(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    o1 = (
        await client.post(
            f"{base}/outlines", json={"volume_no": 1, "chapter_no": 1, "title": "启航", "summary": "飞船出发"}
        )
    ).json()
    o2 = (await client.post(f"{base}/outlines", json={"volume_no": 1, "chapter_no": 2, "title": "异象"})).json()
    return p, base, o1, o2


async def test_create_chapter_from_outline(client):
    _, base, o1, o2 = await _setup(client)
    ch = (await client.post(f"{base}/chapters", json={"outline_id": o1["id"]})).json()
    assert ch["title"] == "启航"
    assert ch["chapter_no"] == 1
    assert ch["status"] == "draft"

    created = (await client.post(f"{base}/chapters/from-outlines", json={})).json()
    assert [c["outline_id"] for c in created] == [o2["id"]]  # 已建章的大纲跳过
    listed = (await client.get(f"{base}/chapters")).json()
    assert [c["chapter_no"] for c in listed] == [1, 2]
    assert "content" not in listed[0]

    # 他项目的大纲不可关联
    resp = await client.post(f"{base}/chapters", json={"outline_id": 99999})
    assert resp.status_code == 400


async def test_manual_edit_updates_word_count(client):
    _, base, _, _ = await _setup(client)
    ch = (await client.post(f"{base}/chapters", json={"title": "手写"})).json()
    assert ch["chapter_no"] == 1
    resp = await client.patch(f"{base}/chapters/{ch['id']}", json={"content": "一二三 四五", "status": "done"})
    assert resp.json()["word_count"] == 5
    assert resp.json()["status"] == "done"
    resp = await client.patch(f"{base}/chapters/{ch['id']}", json={"status": "generating"})
    assert resp.status_code == 422


async def test_generate_requires_ai_config(client):
    _, base, o1, _ = await _setup(client)
    ch = (await client.post(f"{base}/chapters", json={"outline_id": o1["id"]})).json()
    resp = await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={})
    assert resp.status_code == 400
    assert "尚未配置文本模型" in resp.json()["detail"]
    # 校验失败不建任务、不抢占章节
    assert (await client.get("/api/v1/tasks")).json() == []
    assert (await client.get(f"{base}/chapters/{ch['id']}")).json()["status"] == "draft"


async def test_generate_chapter_task_persists(app, client, fake_llm):
    _, base, o1, _ = await _setup(client)
    await create_ai_config(client)
    hero = (await client.post(f"{base}/characters", json={"name": "林舟", "role_type": "主角"})).json()
    ch = (await client.post(f"{base}/chapters", json={"outline_id": o1["id"]})).json()
    selected = await client.put(f"{base}/chapters/{ch['id']}/cast", json={"character_ids": [hero["id"]]})
    assert selected.status_code == 200, selected.text

    text = "夜色深沉，星舰缓缓驶离港口。\n\n林舟站在舰桥上，望着远方。"
    gate = Gate()
    fake_llm.push([gate] + [text[i : i + 7] for i in range(0, len(text), 7)])
    resp = await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 1000})
    assert resp.status_code == 202
    task = resp.json()
    assert task["kind"] == "chapter.generate" and task["chapter_id"] == ch["id"]
    assert task["status"] in ("queued", "running")
    assert task["title"] == "第 1 章 启航 · AI 生成"
    # 创建即抢占：排队 / 运行期间章节处于 generating（编辑器只读）
    assert (await client.get(f"{base}/chapters/{ch['id']}")).json()["status"] == "generating"

    stream = await stream_with_gate(app, client, task["id"], gate)
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.headers["x-accel-buffering"] == "no"
    events = parse_sse(stream.text)
    assert events[0][0] == "snapshot" and events[0][1]["text"] == ""
    deltas = [d["text"] for e, d in events if e == "delta"]
    assert len(deltas) > 1
    assert "".join(deltas) == text
    assert events[-1][0] == "done"
    done = events[-1][1]["result"]["chapter"]
    assert done["status"] == "done"
    assert done["content"] == text
    assert done["word_count"] == count_words(text)
    finished = events[-1][1]["task"]
    assert finished["status"] == "succeeded" and finished["progress"] == 100 and finished["finished_at"]

    saved = (await client.get(f"{base}/chapters/{ch['id']}")).json()
    assert saved["content"] == text and saved["status"] == "done"
    assert (await client.get(f"/api/v1/tasks/{task['id']}/output")).json()["text"] == text

    # 提示词包含大纲梗概与角色
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "飞船出发" in prompt
    assert "■ 林舟" in prompt


async def test_generate_error_restores_status(app, client, fake_llm):
    _, base, o1, _ = await _setup(client)
    await create_ai_config(client)
    ch = (await client.post(f"{base}/chapters", json={"outline_id": o1["id"], "content": "旧稿"})).json()

    fake_llm.push(["部分", AIProviderError("模型调用失败：额度不足")])
    task, events = await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/generate"))
    assert final_event(events) == ("error", {"detail": "模型调用失败：额度不足"})
    assert task["status"] == "failed" and task["error"] == "模型调用失败：额度不足"

    async with app.state.sessionmaker() as db:
        row = await db.scalar(select(Chapter).where(Chapter.id == ch["id"]))
        assert row.status == "draft"
        assert row.content == "旧稿"
    # 已输出的部分文本保留供查看
    assert (await client.get(f"/api/v1/tasks/{task['id']}/output")).json()["text"] == "部分"


async def test_generating_conflict(app, client):
    _, base, o1, _ = await _setup(client)
    await create_ai_config(client)
    ch = (await client.post(f"{base}/chapters", json={"outline_id": o1["id"]})).json()
    async with app.state.sessionmaker() as db:
        row = await db.get(Chapter, ch["id"])
        row.status = "generating"
        await db.commit()
    resp = await client.post(f"/api/v1/chapters/{ch['id']}/generate")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "该章节正在生成中，请稍候再试"
    assert (await client.post(f"/api/v1/chapters/{ch['id']}/polish")).status_code == 409

    # 生成中不允许改正文（否则会被生成结果静默覆盖），但允许单独复位状态
    resp = await client.patch(f"{base}/chapters/{ch['id']}", json={"content": "手改"})
    assert resp.status_code == 409
    resp = await client.patch(f"{base}/chapters/{ch['id']}", json={"status": "draft"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"


async def test_polish(client, fake_llm):
    _, base, _, _ = await _setup(client)
    await create_ai_config(client)
    ch = (await client.post(f"{base}/chapters", json={"title": "草稿"})).json()

    resp = await client.post(f"/api/v1/chapters/{ch['id']}/polish")
    assert resp.status_code == 400  # 空正文不能润色

    await client.patch(f"{base}/chapters/{ch['id']}", json={"content": "他走了。他又回来了。"})
    fake_llm.push("他转身离去，片刻后又折返回来。")
    resp = await client.post(f"/api/v1/chapters/{ch['id']}/polish", json={"instruction": "更有画面感"})
    task, events = await run_task(client, resp)
    assert task["kind"] == "chapter.polish" and task["title"] == "第 1 章 草稿 · AI 润色"
    assert events[-1][0] == "done"
    assert events[-1][1]["result"]["chapter"]["content"] == "他转身离去，片刻后又折返回来。"
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "他走了。他又回来了。" in prompt
    assert "更有画面感" in prompt


async def test_explicit_ai_config_id(client, fake_llm):
    _, base, o1, _ = await _setup(client)
    await create_ai_config(client, name="默认", model="m-default")
    other = await create_ai_config(client, name="备用", model="m-other", provider="anthropic", base_url=None)
    ch = (await client.post(f"{base}/chapters", json={"outline_id": o1["id"]})).json()
    fake_llm.push("正文")
    task, _ = await run_task(
        client, await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"ai_config_id": other["id"]})
    )
    assert task["status"] == "succeeded"
    assert fake_llm.calls[-1]["model"] == "m-other"
    assert fake_llm.calls[-1]["provider"] == "anthropic"
