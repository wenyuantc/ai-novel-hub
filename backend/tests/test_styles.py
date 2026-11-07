"""写作风格库：预设只读可复制、解析优先级（临时 > 章节 > 项目默认 > 无）、各生成入口注入与生成记录、
批量逐章解析与续跑沿用、删除引用回落、提取 / 试写（同步 SSE，不落库）、向导只带一句话描述、旧 style 字段兼容、
导入导出、跨用户隔离。"""

import json

import pytest

from app.models.style_presets import PRESET_STYLES
from app.services import batch_service
from tests import prompt_legacy
from tests.helpers import (
    analysis_json,
    create_ai_config,
    create_project,
    parse_sse,
    register,
    run_task,
    stream_task,
    wait_task,
)

API = "/api/v1/styles"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(batch_service, "RETRY_DELAYS", (0.0, 0.0))


async def _style(client, name: str, **fields) -> dict:
    resp = await client.post(API, json={"name": name, **fields})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _setup(client, *, chapters: int = 1, contents: list[str] | None = None):
    await register(client)
    await create_ai_config(client)
    p = await create_project(client, style="")
    base = f"/api/v1/projects/{p['id']}"
    chs = []
    for i in range(chapters):
        content = (contents or [""] * chapters)[i]
        chs.append((await client.post(f"{base}/chapters", json={"title": f"章{i + 1}", "content": content})).json())
    return p, base, chs


def _user_prompt(call) -> str:
    return call["messages"][-1]["content"]


async def _logs(client, base, ch):
    items = (await client.get(f"{base}/chapters/{ch['id']}/generation-logs")).json()
    return [(await client.get(f"{base}/chapters/{ch['id']}/generation-logs/{i['id']}")).json() for i in items]


# ---------------- 预设与 CRUD ----------------


async def test_presets_readonly_and_copyable(client):
    await register(client)
    styles = (await client.get(API)).json()
    presets = [s for s in styles if s["is_preset"]]
    assert [s["preset_key"] for s in presets] == [p["preset_key"] for p in PRESET_STYLES] and len(presets) == 6
    preset = presets[0]
    resp = await client.patch(f"{API}/{preset['id']}", json={"name": "改名"})
    assert resp.status_code == 403 and "预设" in resp.json()["detail"]
    assert (await client.delete(f"{API}/{preset['id']}")).status_code == 403
    copy = (await client.post(f"{API}/{preset['id']}/copy")).json()
    assert copy["is_preset"] is False and copy["name"] == f"{preset['name']}（副本）" and copy["pov"] == preset["pov"]
    again = (await client.post(f"{API}/{preset['id']}/copy")).json()
    assert again["name"] == f"{preset['name']}（副本2）"
    resp = await client.patch(f"{API}/{copy['id']}", json={"tone": "改过的基调"})
    assert resp.status_code == 200 and resp.json()["tone"] == "改过的基调"


async def test_crud_validation(client):
    await register(client)
    s = await _style(client, " 我的风格 ", description="一句话", sample="样" * 2000)
    assert s["name"] == "我的风格" and s["references"] == {"projects": 0, "chapters": 0}
    assert (await client.post(API, json={"name": "x", "sample": "样" * 2001})).status_code == 422
    assert (await client.post(API, json={"name": ""})).status_code == 422
    assert (await client.get(f"{API}/{s['id']}")).json()["description"] == "一句话"
    assert (await client.delete(f"{API}/{s['id']}")).status_code == 204
    assert (await client.get(f"{API}/{s['id']}")).status_code == 404


# ---------------- 解析优先级 ----------------


async def test_resolution_priority_and_generation_log(client, fake_llm):
    p, base, (ch,) = await _setup(client)
    a = await _style(client, "项目风格", extra="项目默认的风格段A")
    b = await _style(client, "章节风格", pov="第一人称", rhythm="长句")
    c = await _style(client, "临时风格", extra="临时风格段C")

    async def gen(**body) -> str:
        fake_llm.push("正文")
        task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/generate", json=body))
        assert task["status"] == "succeeded", task
        return _user_prompt(fake_llm.calls[-1])

    # 无风格
    assert "写作风格：" not in await gen()
    assert (await _logs(client, base, ch))[0]["params"]["style"] is None
    # 项目默认
    assert (await client.patch(base, json={"default_style_id": a["id"]})).json()["default_style"]["name"] == "项目风格"
    assert "写作风格：项目默认的风格段A" in await gen()
    log = (await _logs(client, base, ch))[0]
    assert log["params"]["style"] == {
        "id": a["id"],
        "name": "项目风格",
        "block": "项目默认的风格段A",
        "source": "project",
    }
    # 章节覆盖 > 项目默认
    resp = await client.patch(f"{base}/chapters/{ch['id']}", json={"style_id": b["id"]})
    assert resp.status_code == 200 and resp.json()["style_id"] == b["id"]
    prompt = await gen()
    assert "写作风格：叙事视角：第一人称\n句式节奏：长句" in prompt and "风格段A" not in prompt
    assert (await _logs(client, base, ch))[0]["params"]["style"]["source"] == "chapter"
    # 临时指定 > 章节覆盖
    prompt = await gen(style=c["id"])
    assert "写作风格：临时风格段C" in prompt and "第一人称" not in prompt
    assert (await _logs(client, base, ch))[0]["params"]["style"]["id"] == c["id"]
    # 临时不用风格
    assert "写作风格：" not in await gen(style="none")
    # 章节覆盖清空 → 回到项目默认
    await client.patch(f"{base}/chapters/{ch['id']}", json={"style_id": None})
    assert "风格段A" in await gen()
    # 指定不可见的风格 → 400，不建任务、不抢占
    resp = await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"style": 999999})
    assert resp.status_code == 400 and "写作风格" in resp.json()["detail"]
    assert (await client.get(f"{base}/chapters/{ch['id']}")).json()["status"] != "generating"


async def test_polish_revise_rewrite_use_style(client, fake_llm):
    p, base, (ch,) = await _setup(client, contents=["林远走进山门。守卫拦住了他。"])
    s = await _style(client, "凌厉", extra="凌厉的风格段")
    t = await _style(client, "临时", extra="临时的风格段")
    await client.patch(f"{base}/chapters/{ch['id']}", json={"style_id": s["id"]})

    fake_llm.push("润色后的正文")
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/polish", json={}))
    assert task["status"] == "succeeded"
    assert "写作风格：凌厉的风格段" in _user_prompt(fake_llm.calls[-1])
    assert (await _logs(client, base, ch))[0]["params"]["style"]["id"] == s["id"]

    fake_llm.push(analysis_json())
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/analyze"))
    analysis = (await client.get(f"/api/v1/chapters/{ch['id']}/analyses")).json()[0]
    fake_llm.push("重写后的正文")
    resp = await client.post(
        f"/api/v1/chapters/{ch['id']}/revise",
        json={"analysis_id": analysis["id"], "suggestion_ids": [1], "style": t["id"]},
    )
    task, _ = await run_task(client, resp)
    assert task["status"] == "succeeded"
    assert "写作风格：临时的风格段" in _user_prompt(fake_llm.calls[-1])

    content = (await client.get(f"{base}/chapters/{ch['id']}")).json()["content"]
    fake_llm.push("改好的")
    resp = await client.post(
        f"/api/v1/chapters/{ch['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": len(content)}
    )
    assert parse_sse(resp.text)[-1][0] == "done"
    assert "写作风格：凌厉的风格段" in _user_prompt(fake_llm.calls[-1])
    fake_llm.push("改好的")
    resp = await client.post(
        f"/api/v1/chapters/{ch['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2, "style": "none"}
    )
    assert "写作风格：" not in _user_prompt(fake_llm.calls[-1])
    rewrite_log = (await _logs(client, base, ch))[0]
    assert rewrite_log["kind"] == "rewrite" and rewrite_log["params"]["style"] is None
    assert rewrite_log["params"]["prompt"]["key"] == "chapter.rewrite"
    resp = await client.post(
        f"/api/v1/chapters/{ch['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2, "style": 999999}
    )
    assert resp.status_code == 400


# ---------------- 批量 ----------------


def _gen_prompts(fake_llm) -> list[str]:
    return [_user_prompt(c) for c in fake_llm.calls if "目标字数" in _user_prompt(c)]


async def test_batch_resolves_style_per_chapter(client, fake_llm):
    p, base, (c1, c2) = await _setup(client, chapters=2)
    a = await _style(client, "项目", extra="项目风格段")
    b = await _style(client, "第二章", extra="第二章风格段")
    await client.patch(base, json={"default_style_id": a["id"]})
    await client.patch(f"{base}/chapters/{c2['id']}", json={"style_id": b["id"]})
    fake_llm.push("一")
    fake_llm.push("二")
    resp = await client.post(
        f"{base}/batch-generate",
        json={"start_chapter_id": c1["id"], "end_chapter_id": c2["id"], "auto_continue": False},
    )
    task, _ = await run_task(client, resp)
    assert task["status"] == "succeeded"
    first, second = _gen_prompts(fake_llm)
    assert "写作风格：项目风格段" in first and "第二章风格段" not in first
    assert "写作风格：第二章风格段" in second
    assert (await _logs(client, base, c2))[0]["params"]["style"]["id"] == b["id"]


async def test_batch_explicit_style_kept_across_resume(app, client, fake_llm):
    p, base, (c1, c2) = await _setup(client, chapters=2)
    a = await _style(client, "项目", extra="项目风格段")
    c = await _style(client, "指定", extra="指定风格段")
    await client.patch(base, json={"default_style_id": a["id"]})
    fake_llm.push("一")
    fake_llm.push("二")
    resp = await client.post(
        f"{base}/batch-generate",
        json={
            "start_chapter_id": c1["id"],
            "end_chapter_id": c2["id"],
            "auto_continue": False,
            "review_every": 1,
            "style": c["id"],
        },
    )
    assert resp.status_code == 202
    task_id = resp.json()["id"]
    assert (await stream_task(client, task_id))[-1][0] == "paused"
    async with app.state.sessionmaker() as db:
        from app.models import Task

        assert (await db.get(Task, task_id)).input["request"]["style"] == c["id"]
    assert (await client.post(f"/api/v1/tasks/{task_id}/resume")).status_code == 202
    assert (await wait_task(client, task_id))["status"] == "succeeded"
    prompts = _gen_prompts(fake_llm)
    assert len(prompts) == 2 and all("写作风格：指定风格段" in pr for pr in prompts)
    assert not any("项目风格段" in pr for pr in prompts)
    # 指定不可见的风格 → 400
    resp = await client.post(
        f"{base}/batch-generate", json={"start_chapter_id": c1["id"], "end_chapter_id": c2["id"], "style": 999999}
    )
    assert resp.status_code == 400


async def test_batch_resume_rejects_deleted_explicit_style(client, fake_llm):
    p, base, (c1, c2) = await _setup(client, chapters=2)
    c = await _style(client, "指定", extra="指定风格段")
    fake_llm.push("一")
    resp = await client.post(
        f"{base}/batch-generate",
        json={
            "start_chapter_id": c1["id"],
            "end_chapter_id": c2["id"],
            "auto_continue": False,
            "review_every": 1,
            "style": c["id"],
        },
    )
    task_id = resp.json()["id"]
    assert (await stream_task(client, task_id))[-1][0] == "paused"
    # 暂停期间删掉指定的风格：继续时同步校验 → 400，任务仍待审核
    assert (await client.delete(f"{API}/{c['id']}")).status_code == 204
    resp = await client.post(f"/api/v1/tasks/{task_id}/resume")
    assert resp.status_code == 400 and resp.json()["detail"] == "指定的写作风格不存在或不可用"
    assert (await client.get(f"/api/v1/tasks/{task_id}")).json()["status"] == "paused"


# ---------------- 删除引用回落 ----------------


async def test_delete_referenced_style_falls_back(client, fake_llm):
    p, base, (ch,) = await _setup(client)
    a = await _style(client, "项目", extra="项目风格段")
    b = await _style(client, "章节", extra="章节风格段")
    await client.patch(base, json={"default_style_id": a["id"]})
    await client.patch(f"{base}/chapters/{ch['id']}", json={"style_id": b["id"]})
    listed = {s["id"]: s for s in (await client.get(API)).json()}
    assert listed[b["id"]]["references"] == {"projects": 0, "chapters": 1}
    assert listed[a["id"]]["references"] == {"projects": 1, "chapters": 0}

    resp = await client.delete(f"{API}/{b['id']}")
    assert resp.status_code == 409 and resp.json()["references"] == {"projects": 0, "chapters": 1}
    assert "1 个章节" in resp.json()["detail"]
    assert (await client.delete(f"{API}/{b['id']}", params={"force": "true"})).status_code == 204
    assert (await client.get(f"{base}/chapters/{ch['id']}")).json()["style_id"] is None
    fake_llm.push("正文")
    await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={}))
    assert "写作风格：项目风格段" in _user_prompt(fake_llm.calls[-1])  # 回落到项目默认

    assert (await client.delete(f"{API}/{a['id']}")).status_code == 409
    assert (await client.delete(f"{API}/{a['id']}?force=true")).status_code == 204
    project = (await client.get(base)).json()
    assert project["default_style_id"] is None and project["default_style"] is None


# ---------------- 提取 / 试写 ----------------


SAMPLE = "夜色像一层薄冰，压在港口的灯上。" * 20


async def test_extract_returns_draft_without_saving(client, fake_llm):
    await register(client)
    await create_ai_config(client)
    before = len((await client.get(API)).json())
    fake_llm.push(
        json.dumps({"name": "冷夜", "description": "冷", "pov": "第三人称", "rhythm": "短句", "tone": "冷" * 600})
    )
    resp = await client.post(f"{API}/extract", json={"sample_text": SAMPLE})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    assert [e for e, _ in events].count("delta") > 1 and events[-1][0] == "done"
    draft = events[-1][1]["draft"]
    assert (draft["name"], draft["pov"], len(draft["tone"])) == ("冷夜", "第三人称", 500)
    assert events[-1][1]["warnings"]
    assert SAMPLE.strip() in _user_prompt(fake_llm.calls[-1])
    assert len((await client.get(API)).json()) == before  # 不落库

    fake_llm.push("不是 JSON")
    events = parse_sse((await client.post(f"{API}/extract", json={"sample_text": SAMPLE})).text)
    assert events[-1][0] == "error" and "解析失败" in events[-1][1]["detail"]
    assert (await client.post(f"{API}/extract", json={"sample_text": "太短"})).status_code == 422


async def test_trial_streams_without_touching_chapters(client, fake_llm):
    p, base, (ch,) = await _setup(client, contents=["原文"])
    s = await _style(client, "试写", pov="第一人称")
    fake_llm.push("试写出来的一段正文")
    resp = await client.post(f"{API}/trial", json={"style_id": s["id"], "scene": "雨夜码头", "words": 300})
    events = parse_sse(resp.text)
    assert events[-1] == ("done", {"text": "试写出来的一段正文"})
    prompt = _user_prompt(fake_llm.calls[-1])
    assert "叙事视角：第一人称" in prompt and "雨夜码头" in prompt and "约 300 字" in prompt
    # 未保存的风格字段
    fake_llm.push("草稿试写")
    resp = await client.post(f"{API}/trial", json={"style": {"extra": "草稿风格"}, "scene": "山门"})
    assert parse_sse(resp.text)[-1][0] == "done" and "草稿风格" in _user_prompt(fake_llm.calls[-1])
    assert (await client.get(f"{base}/chapters/{ch['id']}")).json()["content"] == "原文"
    assert (await client.get(f"{base}/chapters/{ch['id']}/generation-logs")).json() == []
    assert (await client.post(f"{API}/trial", json={"scene": "x"})).status_code == 400
    assert (await client.post(f"{API}/trial", json={"style": {}, "scene": "x"})).status_code == 400
    assert (await client.post(f"{API}/trial", json={"style_id": s["id"], "scene": "x", "words": 50})).status_code == 422


# ---------------- 向导 / 实体只用一句话描述 ----------------


async def test_wizard_and_entity_use_description_only(client, fake_llm):
    p, base, _ = await _setup(client)
    s = await _style(client, "长风格", description="一句话的风格描述", extra="很长的自由补充EXTRA", pov="第一人称")
    await client.patch(base, json={"default_style_id": s["id"]})
    fake_llm.push('[{"title": "设定", "content": "内容"}]')
    task, _ = await run_task(client, await client.post(f"{base}/wizard/world", json={}))
    assert task["status"] == "succeeded"
    prompt = _user_prompt(fake_llm.calls[-1])
    assert "写作风格：一句话的风格描述" in prompt and "EXTRA" not in prompt and "第一人称" not in prompt
    fake_llm.push('{"character": {"name": "新角色"}, "relationships": []}')
    task, _ = await run_task(client, await client.post(f"{base}/characters/generate", json={}))
    assert task["status"] == "succeeded"
    prompt = _user_prompt(fake_llm.calls[-1])
    assert "写作风格：一句话的风格描述" in prompt and "EXTRA" not in prompt


# ---------------- 旧 style 字段兼容 ----------------


async def test_legacy_style_field_creates_original_style(client, fake_llm):
    await register(client)
    await create_ai_config(client)
    p = await create_project(client, title="剑道", style="  凌厉  ")
    assert "style" not in p
    assert p["default_style"]["name"] == "《剑道》原风格" and p["notice"]
    base = f"/api/v1/projects/{p['id']}"
    # 更新同一个原风格
    resp = await client.patch(base, json={"style": "更凌厉"})
    assert resp.json()["default_style_id"] == p["default_style_id"] and resp.json()["notice"]
    style = (await client.get(f"{API}/{p['default_style_id']}")).json()
    assert (style["extra"], style["description"]) == ("更凌厉", "更凌厉")
    # 迁移过来的原风格：章节提示词中的风格行与改造前一致
    ch = (await client.post(f"{base}/chapters", json={"title": "山门"})).json()
    fake_llm.push("正文")
    await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={}))
    messages = fake_llm.calls[-1]["messages"]
    user = messages[-1]["content"]
    context = user.split("\n\n【本章任务】")[0].removeprefix("以下是创作参考资料：\n")
    legacy = prompt_legacy.build_chapter_messages(
        context=context, chapter_no=1, title="山门", summary="", target_words=3000, style="更凌厉"
    )
    # 新锁定范围是必要的独立增量；此前风格段和其余旧消息仍须逐字符相同。
    legacy_user, boundary, scene_scope = user.partition("\n\n【场景写作范围（锁定）】")
    assert boundary and scene_scope
    assert [{**message, "content": legacy_user} if index == len(messages) - 1 else message
            for index, message in enumerate(messages)] == legacy
    assert user.count("【场景写作范围（锁定）】") == 1
    assert "只输出当前目标场景的正文" in scene_scope and "本场景目标字数：3000" in scene_scope
    assert "写作风格：更凌厉" in context  # 作品信息里的风格行同样不变
    # 空串：取消默认（原风格保留）
    resp = await client.patch(base, json={"style": ""})
    assert resp.json()["default_style_id"] is None
    assert (await client.get(f"{API}/{p['default_style_id']}")).status_code == 200


# ---------------- 导入导出 ----------------


async def test_export_import_styles(client, make_client):
    await register(client)
    a = await _style(client, "甲", pov="第一人称", sample="示例")
    await _style(client, "乙")
    exported = (await client.get(f"{API}/export", params={"ids": [a["id"]]})).json()
    assert exported["format"] == "ainovel.styles" and [i["name"] for i in exported["items"]] == ["甲"]
    assert "id" not in exported["items"][0]
    everything = (await client.get(f"{API}/export")).json()
    assert [i["name"] for i in everything["items"]] == ["甲", "乙"]

    result = (await client.post(f"{API}/import", json={"data": exported})).json()
    assert [(i["name"], i["renamed_from"]) for i in result["items"]] == [("甲（导入）", "甲")]
    result = (await client.post(f"{API}/import", json={"data": exported})).json()
    assert result["items"][0]["name"] == "甲（导入2）"

    bob = make_client()
    await register(bob, "bob")
    data = {**exported, "items": [*exported["items"], {"pov": "无名"}]}
    result = (await bob.post(f"{API}/import", json={"data": data})).json()
    assert [i["name"] for i in result["items"]] == ["甲"] and result["warnings"]
    mine = [s for s in (await bob.get(API)).json() if not s["is_preset"]]
    assert [(s["name"], s["pov"], s["sample"]) for s in mine] == [("甲", "第一人称", "示例")]

    assert (await bob.post(f"{API}/import", json={"data": {"format": "nope"}})).status_code == 400
    big = {**exported, "items": [{"name": "大", "extra": "长" * 400_000}]}
    resp = await bob.post(f"{API}/import", json={"data": big})
    assert resp.status_code == 400 and "1MB" in resp.json()["detail"]


# ---------------- 跨用户隔离 ----------------


async def test_styles_cross_user_isolation(client, make_client):
    p, base, (ch,) = await _setup(client)
    s = await _style(client, "alice 的风格")
    await client.patch(base, json={"default_style_id": s["id"]})

    bob = make_client()
    await register(bob, "bob")
    assert all(x["is_preset"] for x in (await bob.get(API)).json())
    for method, url in (
        ("GET", f"{API}/{s['id']}"),
        ("PATCH", f"{API}/{s['id']}"),
        ("DELETE", f"{API}/{s['id']}"),
        ("POST", f"{API}/{s['id']}/copy"),
    ):
        resp = await bob.request(method, url, json={} if method in ("PATCH", "POST") else None)
        assert resp.status_code == 404, (method, url, resp.text)
    resp = await bob.post(f"{API}/trial", json={"style_id": s["id"], "scene": "x"})
    assert resp.status_code == 404
    assert (await bob.get(f"{API}/export", params={"ids": [s["id"]]})).json()["items"] == []
    # bob 不能把 alice 的风格设为自己项目 / 章节的风格
    bp = await create_project(bob, style="")
    resp = await bob.patch(f"/api/v1/projects/{bp['id']}", json={"default_style_id": s["id"]})
    assert resp.status_code == 400
    bch = (await bob.post(f"/api/v1/projects/{bp['id']}/chapters", json={"title": "x"})).json()
    resp = await bob.patch(f"/api/v1/projects/{bp['id']}/chapters/{bch['id']}", json={"style_id": s["id"]})
    assert resp.status_code == 400
    # alice 数据不变
    assert (await client.get(f"{API}/{s['id']}")).json()["references"]["projects"] == 1


async def test_sync_presets_upserts_by_key(app, client):
    from sqlalchemy import delete, select, update

    from app.models import WritingStyle
    from app.services import style_service

    async with app.state.sessionmaker() as db:
        await db.execute(update(WritingStyle).where(WritingStyle.preset_key == "lean_cold").values(name="被改过"))
        await db.execute(delete(WritingStyle).where(WritingStyle.preset_key == "light_humor"))
        await db.commit()
    await style_service.sync_presets(app.state.sessionmaker)
    async with app.state.sessionmaker() as db:
        rows = (await db.scalars(select(WritingStyle).where(WritingStyle.user_id.is_(None)))).all()
    by_key = {r.preset_key: r for r in rows}
    assert set(by_key) == {p["preset_key"] for p in PRESET_STYLES}
    assert by_key["lean_cold"].name == PRESET_STYLES[0]["name"]
