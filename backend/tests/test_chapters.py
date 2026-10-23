from sqlalchemy import select

from app.ai.providers import AIProviderError
from app.models import Chapter
from app.services.chapter_service import count_words
from tests.helpers import create_ai_config, create_project, parse_sse, register


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
    assert "尚未配置 AI 模型" in resp.json()["detail"]


async def test_generate_chapter_sse_persists(app, client, fake_llm):
    _, base, o1, _ = await _setup(client)
    await create_ai_config(client)
    await client.post(f"{base}/characters", json={"name": "林舟", "role_type": "主角"})
    ch = (await client.post(f"{base}/chapters", json={"outline_id": o1["id"]})).json()

    text = "夜色深沉，星舰缓缓驶离港口。\n\n林舟站在舰桥上，望着远方。"
    fake_llm.push(text)
    resp = await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 1000})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-accel-buffering"] == "no"

    events = parse_sse(resp.text)
    deltas = [d["text"] for e, d in events if e == "delta"]
    assert len(deltas) > 1
    assert "".join(deltas) == text
    assert events[-1][0] == "done"
    done = events[-1][1]["chapter"]
    assert done["status"] == "done"
    assert done["content"] == text
    assert done["word_count"] == count_words(text)

    saved = (await client.get(f"{base}/chapters/{ch['id']}")).json()
    assert saved["content"] == text and saved["status"] == "done"

    # 提示词包含大纲梗概与角色
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "飞船出发" in prompt
    assert "林舟" in prompt


async def test_generate_error_restores_status(app, client, fake_llm):
    _, base, o1, _ = await _setup(client)
    await create_ai_config(client)
    ch = (await client.post(f"{base}/chapters", json={"outline_id": o1["id"], "content": "旧稿"})).json()

    fake_llm.push(["部分", AIProviderError("模型调用失败：额度不足")])
    resp = await client.post(f"/api/v1/chapters/{ch['id']}/generate")
    events = parse_sse(resp.text)
    assert events[-1] == ("error", {"detail": "模型调用失败：额度不足"})

    async with app.state.sessionmaker() as db:
        row = await db.scalar(select(Chapter).where(Chapter.id == ch["id"]))
        assert row.status == "draft"
        assert row.content == "旧稿"


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
    events = parse_sse(resp.text)
    assert events[-1][0] == "done"
    assert events[-1][1]["chapter"]["content"] == "他转身离去，片刻后又折返回来。"
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "他走了。他又回来了。" in prompt
    assert "更有画面感" in prompt


async def test_explicit_ai_config_id(client, fake_llm):
    _, base, o1, _ = await _setup(client)
    await create_ai_config(client, name="默认", model="m-default")
    other = await create_ai_config(client, name="备用", model="m-other", provider="anthropic", base_url=None)
    ch = (await client.post(f"{base}/chapters", json={"outline_id": o1["id"]})).json()
    fake_llm.push("正文")
    await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"ai_config_id": other["id"]})
    assert fake_llm.calls[-1]["model"] == "m-other"
    assert fake_llm.calls[-1]["provider"] == "anthropic"
