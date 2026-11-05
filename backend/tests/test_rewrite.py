import pytest
from sqlalchemy import select

from app.ai.prompts.registry import get_template
from app.ai.providers import AIProviderError
from app.models import Chapter, Character, Project, PromptTemplateOverride, SettingProgression, SettingTimeline
from tests.helpers import create_ai_config, create_project, parse_sse, register

# 含 emoji 与 CJK 扩展 B 区生僻字（UTF-16 下各占 2 个码元），偏移按码点计
CONTENT = "开头😀𠀀一段。需要改写的句子。结尾🎉。"
SELECTED = "需要改写的句子"
START = CONTENT.index(SELECTED)
END = START + len(SELECTED)


async def _setup(client, content: str = CONTENT):
    await register(client)
    p = await create_project(client, style="冷峻克制")
    await create_ai_config(client)
    base = f"/api/v1/projects/{p['id']}"
    ch = (await client.post(f"{base}/chapters", json={"title": "第一章", "content": content})).json()
    return ch, f"{base}/chapters/{ch['id']}", f"/api/v1/chapters/{ch['id']}/rewrite"


async def test_rewrite_preview_then_apply(client, fake_llm):
    ch, url, rewrite = await _setup(client)
    fake_llm.push("改好的句子")
    resp = await client.post(
        rewrite, json={"op": "rewrite", "start": START, "end": END, "instruction": "更紧凑"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    assert events[-1][0] == "done"
    done = events[-1][1]
    assert (done["text"], done["start"], done["end"], done["original"]) == ("改好的句子", START, END, SELECTED)
    assert isinstance(done["log_id"], int)

    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert SELECTED in prompt and "开头😀𠀀一段。" in prompt and "结尾🎉。" in prompt
    assert "更紧凑" in prompt and "冷峻克制" in prompt

    # 预览不改正文、不写版本、不改状态
    saved = (await client.get(url)).json()
    assert saved["content"] == CONTENT and saved["status"] == "draft"
    assert len((await client.get(f"{url}/versions")).json()) == 1

    log = (await client.get(f"{url}/generation-logs")).json()[0]
    assert log["id"] == done["log_id"]
    assert (log["kind"], log["status"], log["output_chars"]) == ("rewrite", "success", 5)
    assert log["params"]["selection_chars"] == len(SELECTED)

    resp = await client.post(
        f"{url}/rewrite/apply",
        json={"start": START, "end": END, "original": SELECTED, "replacement": "改好的句子", "log_id": done["log_id"]},
    )
    assert resp.status_code == 200
    expected = CONTENT[:START] + "改好的句子" + CONTENT[END:]
    assert expected == "开头😀𠀀一段。改好的句子。结尾🎉。"
    assert resp.json()["content"] == expected
    assert resp.json()["word_count"] == len(expected)
    vs = (await client.get(f"{url}/versions")).json()
    assert [(v["source"], v["generation_log_id"]) for v in vs] == [("rewrite", done["log_id"]), ("manual", None)]


async def test_apply_rejects_stale_selection(client):
    _, url, _ = await _setup(client)
    # 预览后正文被修改并保存
    await client.patch(url, json={"content": "前面插入了文字。" + CONTENT})
    resp = await client.post(
        f"{url}/rewrite/apply", json={"start": START, "end": END, "original": SELECTED, "replacement": "x"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "正文已变化，请重新选择后再改写"
    # 越界同样视为正文已变化
    resp = await client.post(
        f"{url}/rewrite/apply", json={"start": 999, "end": 999, "original": "", "replacement": "x"}
    )
    assert resp.status_code == 409


async def test_apply_ignores_foreign_log_and_writes_baseline(app, client):
    ch, url, _ = await _setup(client)
    async with app.state.sessionmaker() as db:
        row = await db.get(Chapter, ch["id"])
        row.content = CONTENT + "未保存"  # 存量正文未被版本保存
        await db.commit()
    resp = await client.post(
        f"{url}/rewrite/apply",
        json={"start": START, "end": END, "original": SELECTED, "replacement": "新句", "log_id": 99999},
    )
    assert resp.status_code == 200
    vs = (await client.get(f"{url}/versions")).json()
    assert [(v["source"], v["generation_log_id"]) for v in vs] == [
        ("rewrite", None),
        ("baseline", None),
        ("manual", None),
    ]


async def test_continue_at_cursor(client, fake_llm):
    _, url, rewrite = await _setup(client)
    fake_llm.push("接着写的内容")
    resp = await client.post(rewrite, json={"op": "continue", "start": END, "end": END})
    done = parse_sse(resp.text)[-1]
    assert done[0] == "done" and done[1]["original"] == "" and done[1]["start"] == END
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert CONTENT[:END] in prompt and "结尾🎉" not in prompt  # 续写只取光标前

    assert done[1]["before"] == CONTENT[:END]

    resp = await client.post(
        f"{url}/rewrite/apply",
        json={"start": END, "end": END, "original": "", "before": done[1]["before"], "replacement": "接着写的内容"},
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == CONTENT[:END] + "接着写的内容" + CONTENT[END:]
    log = (await client.get(f"{url}/generation-logs")).json()[0]
    assert log["kind"] == "continue"


async def test_continue_apply_rejects_changed_prefix(client, fake_llm):
    """光标续写 original 为空：预览后光标前正文被改（长度不变），靠 before 发现并 409。"""
    long_content = "甲" * 300 + "乙。" + CONTENT
    _, url, rewrite = await _setup(client, long_content)
    cursor = len(long_content) - 3
    fake_llm.push("续写")
    done = parse_sse((await client.post(rewrite, json={"op": "continue", "start": cursor, "end": cursor})).text)[-1][1]
    # before 只取光标前 200 码点
    assert done["before"] == long_content[cursor - 200 : cursor]
    body = {"start": cursor, "end": cursor, "original": "", "before": done["before"], "replacement": "续写"}

    changed = long_content.replace("需要改写", "已经改过")  # 同长度修改光标前的文字
    assert len(changed) == len(long_content)
    assert (await client.patch(url, json={"content": changed})).status_code == 200
    resp = await client.post(f"{url}/rewrite/apply", json=body)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "正文已变化，请重新选择后再改写"

    # 光标位置比 before 还靠前（前文被删短）同样 409，而不是按负下标误匹配
    assert (await client.patch(url, json={"content": "短" * 10 + long_content[-20:]})).status_code == 200
    resp = await client.post(f"{url}/rewrite/apply", json={**body, "start": 5, "end": 5})
    assert resp.status_code == 409

    # 恢复原文后可以采纳
    assert (await client.patch(url, json={"content": long_content})).status_code == 200
    resp = await client.post(f"{url}/rewrite/apply", json=body)
    assert resp.status_code == 200
    assert resp.json()["content"] == long_content[:cursor] + "续写" + long_content[cursor:]


async def test_rewrite_validation(app, client, fake_llm):
    ch, url, rewrite = await _setup(client)
    cases = [
        ({"op": "rewrite", "start": 0, "end": len(CONTENT) + 1}, "选区超出正文范围"),
        ({"op": "rewrite", "start": 5, "end": 3}, "选区超出正文范围"),
        ({"op": "expand", "start": 3, "end": 3}, "请先选中"),
        ({"op": "continue", "start": 0, "end": 0}, "光标前没有正文"),
        ({"op": "continue", "start": 0, "end": 3}, "不要选中文字"),
    ]
    for body, msg in cases:
        resp = await client.post(rewrite, json=body)
        assert resp.status_code == 400, body
        assert msg in resp.json()["detail"]
    resp = await client.post(rewrite, json={"op": "unknown", "start": 0, "end": 1})
    assert resp.status_code == 422

    long_text = "字" * 5001
    await client.patch(url, json={"content": long_text})
    resp = await client.post(rewrite, json={"op": "condense", "start": 0, "end": 5001})
    assert resp.status_code == 400 and "5000" in resp.json()["detail"]
    assert (await client.post(rewrite, json={"op": "condense", "start": 0, "end": 5000})).status_code == 200

    async with app.state.sessionmaker() as db:
        row = await db.get(Chapter, ch["id"])
        row.status = "generating"
        await db.commit()
    assert (await client.post(rewrite, json={"op": "rewrite", "start": 0, "end": 2})).status_code == 409
    resp = await client.post(
        f"{url}/rewrite/apply", json={"start": 0, "end": 2, "original": "字字", "replacement": "x"}
    )
    assert resp.status_code == 409
    assert len(fake_llm.calls) == 1


async def test_rewrite_error_logged(client, fake_llm):
    _, url, rewrite = await _setup(client)
    fake_llm.push(AIProviderError("模型调用失败：超时"))
    events = parse_sse((await client.post(rewrite, json={"op": "deslop", "start": START, "end": END})).text)
    assert events[-1] == ("error", {"detail": "模型调用失败：超时"})
    log = (await client.get(f"{url}/generation-logs")).json()[0]
    assert (log["kind"], log["status"], log["error"]) == ("deslop", "error", "模型调用失败：超时")
    assert (await client.get(url)).json()["content"] == CONTENT


@pytest.mark.parametrize("op", ["rewrite", "expand", "condense", "deslop", "continue"])
@pytest.mark.parametrize("legacy_override", [False, True], ids=["default", "legacy_override"])
async def test_rewrite_uses_early_chapter_setting_and_selection_cast(app, client, fake_llm, op, legacy_override):
    content = "夜雨😀𠀀中，陆无尘拔剑迎敌，风过长街。"
    selected = "陆无尘拔剑迎敌"
    start = content.index(selected)
    end = start + len(selected)
    chapter, url, rewrite = await _setup(client, content)
    assert (await client.patch(url, json={"chapter_no": 5})).status_code == 200
    base = f"/api/v1/projects/{chapter['project_id']}"
    later = await client.post(f"{base}/chapters", json={"title": "晋升", "chapter_no": 20})
    assert later.status_code == 201, later.text

    # 配角只在选区／邻近正文里出现，不在梗概、手选角色或前一章里出现。
    character = await client.post(
        f"{base}/characters", json={"name": "陆无尘", "role_type": "配角", "current_state": "金丹后期"}
    )
    assert character.status_code == 201, character.text
    character_id = character.json()["id"]
    async with app.state.sessionmaker() as db:
        row = await db.get(Character, character_id)
        timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.subject_kind == "character", SettingTimeline.character_id == character_id
            )
        )
        if timeline is None:
            timeline = SettingTimeline(project_id=chapter["project_id"], subject_kind="character", character_id=row.id)
            db.add(timeline)
        timeline.baseline = {
            key: getattr(row, key)
            for key in ("profile", "appearance", "personality", "background", "age", "gender", "life_status")
        } | {"current_state": "练气中期"}
        await db.flush()
        db.add(
            SettingProgression(
                timeline_id=timeline.id,
                field_key="current_state",
                operation="replace",
                value="金丹后期",
                anchor_kind="chapter",
                chapter_id=later.json()["id"],
                boundary="end",
                source="manual",
                protection="manual",
            )
        )
        if legacy_override:
            project = await db.get(Project, chapter["project_id"])
            template = get_template("chapter.rewrite")
            # 模拟升级前已保存的覆盖，不能通过当前 API 再保存缺少锁定变量的模板。
            old_user = template.user.replace("{{ setting_context }}", "") + "\n旧覆盖正文标记"
            db.add(
                PromptTemplateOverride(
                    user_id=project.user_id,
                    key="chapter.rewrite",
                    system=template.system,
                    user=old_user,
                    base_system=template.system,
                    base_user=old_user,
                    base_fingerprint="old",
                )
            )
        await db.commit()

    fake_llm.push("改写结果")
    payload = {"op": op, "start": end if op == "continue" else start, "end": end}
    resp = await client.post(rewrite, json=payload)
    assert resp.status_code == 200, resp.text
    event, done = parse_sse(resp.text)[-1]
    assert event == "done"
    assert (done["start"], done["end"], done["original"]) == (
        payload["start"],
        end,
        "" if op == "continue" else selected,
    )
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "【本章有效设定】" in prompt
    assert "■ 陆无尘" in prompt and "练气中期" in prompt
    assert "金丹后期" not in prompt
    assert "冷峻克制" in prompt
    if legacy_override:
        assert "旧覆盖正文标记" in prompt

    # 同一片段移到晋升之后，必须真的应用第20章的事件，不能始终返回基础值。
    assert (await client.patch(url, json={"chapter_no": 21})).status_code == 200
    fake_llm.push("改写结果")
    after_promotion = await client.post(rewrite, json=payload)
    assert after_promotion.status_code == 200
    assert parse_sse(after_promotion.text)[-1][0] == "done"
    later_prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "■ 陆无尘" in later_prompt and "金丹后期" in later_prompt
    assert "练气中期" not in later_prompt


async def test_rewrite_setting_context_obeys_the_model_context_budget(app, client, fake_llm):
    app.state.settings.context_char_budget = 1000
    chapter, _, rewrite = await _setup(client)
    base = f"/api/v1/projects/{chapter['project_id']}"
    world = await client.post(f"{base}/world", json={"title": "浩瀚星海", "content": "浩瀚" * 2500})
    assert world.status_code == 201, world.text

    fake_llm.push("改写结果")
    resp = await client.post(rewrite, json={"op": "rewrite", "start": START, "end": END})
    assert resp.status_code == 200
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    marker = "【本章有效设定】"
    assert marker in prompt
    assert len(marker + prompt.split(marker, 1)[1]) <= 1000
