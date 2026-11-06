"""用真实 HTTP + FakeLLM 观察智能体工具及历史实际发送的数据。"""

import json

import pytest
from sqlalchemy import select

from app.models import AgentConversation, AgentMessage, AIConfig, AnalysisChange, ChapterAnalysis, Project
from app.services.agent.context import build_messages, tool_result
from app.services.agent.history import HistorySealedError
from tests.helpers import Gate, wait_task
from tests.test_agent_runtime import call, send, setup_agent
from tests.test_setting_fields import _definition, _fields, _versions


async def _character(client, project_url, name, **extra):
    response = await client.post(project_url + "/characters", json={"name": name, **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def _set_visibility(client, project_url, ident, **values):
    url = f"{project_url}/setting-entries/character/{ident}/ai-settings"
    settings = await client.get(url)
    assert settings.status_code == 200, settings.text
    response = await client.patch(
        url, json={"expected_ai_visibility_revision": settings.json()["ai_visibility_revision"], **values},
    )
    assert response.status_code == 200, response.text


def _outgoing_results(fake_llm):
    return {
        message["tool_call_id"]: json.loads(message["content"])["data"]
        for message in fake_llm.tool_calls[-1]["messages"] if message["role"] == "tool"
    }


async def test_agent_scope_filters_directory_fields_links_and_hidden_tool_writes(client, fake_llm):
    project, _, url = await setup_agent(client)
    pid = project["id"]
    project_url = f"/api/v1/projects/{pid}"
    forbidden = await _character(client, project_url, "A-Secret", profile="never-send-profile")
    visible = await _character(
        client, project_url, "B-Visible", aliases=["窗口人物"], background="never-send-background",
        personality="keep-visible-personality",
    )
    manual = await _character(client, project_url, "C-Manual")
    await _set_visibility(client, project_url, forbidden["id"], ai_mode="never")
    await _set_visibility(client, project_url, manual["id"], ai_mode="manual_only")
    await _set_visibility(client, project_url, visible["id"], ai_hidden_builtin_fields=["background"])
    secret = await _definition(client, project_url, label="never-send-field-label", ai_hidden=True)
    public = await _definition(client, project_url, revision=secret["fields_revision"], label="keep-field-label")
    fields = await _fields(client, project_url, visible)
    root = f"{project_url}/setting-entries/character/{visible['id']}/fields"
    saved = await client.patch(
        f"{root}/{secret['item']['id']}/baseline",
        json={**_versions(fields), "field_type": "line", "value": "never-send-field-value"},
    )
    assert saved.status_code == 200, saved.text
    foreshadow = await client.post(
        project_url + "/foreshadows",
        json={"title": "窗边伏笔", "character_ids": [forbidden["id"], visible["id"]]},
    )
    assert foreshadow.status_code == 201, foreshadow.text
    fake_llm.push_tools([
        call("list_setting_entries", {"types": ["character"], "page_size": 1}, "catalog"),
        call("get_character", {"id": visible["id"]}, "visible"),
        call("get_character", {"id": forbidden["id"]}, "never"),
        call("get_character", {"id": manual["id"]}, "manual"),
        call("list_setting_field_definitions", {}, "definitions"),
        call("get_setting_entry_fields", {"entry": {"kind": "character", "id": visible["id"]}}, "fields"),
        call("list_foreshadows", {}, "foreshadows"),
        call("update_character", {"id": visible["id"], "background": "不允许模型覆盖"}, "hidden-update"),
    ])
    fake_llm.push_tools([("text", {"text": "完成"})])
    accepted = await client.post(url + "/messages", json={
        "content": "请查询窗口人物、A-Secret 和 C-Manual",
        "page_context": {
            "character_id": forbidden["id"],
            "route": f"/projects/{pid}/characters/{forbidden['id']}?field=never-send-background",
        },
    })
    assert accepted.status_code == 202, accepted.text
    assert (await wait_task(client, accepted.json()["task"]["id"]))["status"] == "succeeded"
    results = _outgoing_results(fake_llm)
    assert results["catalog"]["total"] == 1
    assert [item["reference"]["id"] for item in results["catalog"]["items"]] == [visible["id"]]
    assert results["visible"]["personality"] == "keep-visible-personality"
    assert "background" not in results["visible"]
    assert results["never"] == results["manual"] == results["hidden-update"] == {"error": "不存在"}
    assert [item["id"] for item in results["definitions"]["items"]] == [public["item"]["id"]]
    assert [item["label"] for item in results["fields"]["fields"]] == ["keep-field-label"]
    assert results["foreshadows"]["items"][0]["character_names"] == ["B-Visible"]
    assert "never-send" not in json.dumps(results, ensure_ascii=False)
    page = fake_llm.tool_calls[0]["messages"][0]["content"]
    assert "never-send" not in page and '"character_id"' not in page
    assert (await client.get(url)).json()["changes"] == []
    assert "never-send-field-value" in (await client.get(root)).text


async def test_write_confirmation_keeps_author_full_result_but_only_replays_minimal_ack(client, fake_llm):
    project, base, url = await setup_agent(client)
    project_url = f"/api/v1/projects/{project['id']}"
    character = await _character(client, project_url, "需要修改", background="private-write-result")
    await _set_visibility(client, project_url, character["id"], ai_hidden_builtin_fields=["background"])
    fake_llm.push_tools([call("update_character", {"id": character["id"], "personality": "谨慎"}, "edit")])
    fake_llm.push_tools([("text", {"text": "已保存"})])
    task_id = await send(client, url, "请修改需要修改角色的性格")
    assert (await wait_task(client, task_id, statuses=("paused", "failed")))["status"] == "paused"
    result = await client.post(base + f"/turns/{task_id}/confirm-all")
    assert result.status_code == 200, result.text
    assert (await wait_task(client, task_id))["status"] == "succeeded"
    assert _outgoing_results(fake_llm)["edit"] == {"ok": True}
    human = (await client.get(url)).json()
    assert human["changes"][0]["result"]["background"] == "private-write-result"


async def test_tightening_seals_old_summary_and_pending_task_but_not_empty_conversation(client, app, fake_llm):
    project, base, url = await setup_agent(client)
    pid = project["id"]
    project_url = f"/api/v1/projects/{pid}"
    character = await _character(client, project_url, "原有档案")
    empty = (await client.post(base + "/conversations", json={})).json()
    empty_url = base + f"/conversations/{empty['id']}"
    cid = int(url.rsplit("/", 1)[1])
    async with app.state.sessionmaker() as db:
        conversation = await db.get(AgentConversation, cid)
        conversation.summary = "previous-summary-sentinel"
        db.add(AgentMessage(
            conversation_id=cid, role="tool", content=tool_result({"previous": "previous-tool-sentinel"}),
        ))
        await db.commit()
    fake_llm.push_tools([call("create_character", {"name": "不得执行"})])
    task_id = await send(client, url)
    assert (await wait_task(client, task_id, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(url)).json()["changes"][0]
    await _set_visibility(client, project_url, character["id"], ai_hidden_builtin_fields=["background"])
    detail = (await client.get(url)).json()
    assert detail["history_sealed"] and detail["summary"] == "previous-summary-sentinel"
    assert "previous-tool-sentinel" in json.dumps(detail, ensure_ascii=False)
    assert not (await client.get(empty_url)).json()["history_sealed"]
    for path, body in (
        (url + "/messages", {"content": "继续"}),
        (base + f"/changes/{change['id']}/confirm", None),
        (base + f"/turns/{task_id}/confirm-all", None),
        (f"/api/v1/tasks/{task_id}/resume", None),
    ):
        response = await client.post(path, json=body)
        assert response.status_code == 409 and response.json()["code"] == "ai_history_sealed", response.text
    assert (await client.post(base + f"/turns/{task_id}/reject-all")).status_code == 200
    assert (await wait_task(client, task_id))["status"] == "cancelled"
    retry = await client.post(f"/api/v1/tasks/{task_id}/retry")
    assert retry.status_code == 409 and retry.json()["code"] == "ai_history_sealed"
    # 直接摘要入口同样先拒绝；不依赖前端封存展示。
    async with app.state.sessionmaker() as db:
        conversation = await db.get(AgentConversation, cid)
        cfg = await db.scalar(select(AIConfig))
        with pytest.raises(HistorySealedError):
            await build_messages(
                db, conversation, await db.get(Project, pid), cfg, fake_llm.factory("openai_compat", "x", None, 5),
            )
    assert len(fake_llm.calls) == 1
    fake_llm.push_tools([("text", {"text": "从干净上下文继续"})])
    assert (await wait_task(client, await send(client, empty_url)))["status"] == "succeeded"
    assert "previous-" not in json.dumps(fake_llm.tool_calls[-1]["messages"])


async def test_tightening_during_agent_reply_stops_tools_and_second_dispatch(client, fake_llm):
    project, _, url = await setup_agent(client, auto=True)
    project_url = f"/api/v1/projects/{project['id']}"
    character = await _character(client, project_url, "正在交谈", background="old-turn-background")
    first_reply = Gate()
    fake_llm.push_tools([first_reply, call("update_character", {"id": character["id"], "personality": "新性格"})])
    fake_llm.push_tools([("text", {"text": "不能开始第二次调用"})])
    task_id = await send(client, url, "修改正在交谈的性格")
    await first_reply.wait_reached()
    try:
        await _set_visibility(client, project_url, character["id"], ai_hidden_builtin_fields=["background"])
    finally:
        first_reply.release()
    task = await wait_task(client, task_id)
    assert task["status"] == "failed" and "AI 可见设置已变更" in task["error"], task
    detail = (await client.get(url)).json()
    assert detail["history_sealed"] and not detail["changes"]
    assert len(fake_llm.tool_calls) == 1
    saved = (await client.get(project_url + "/characters")).json()[0]
    assert saved["personality"] == "" and saved["background"] == "old-turn-background"


@pytest.mark.parametrize(
    ("competitor_alias", "previous", "next_values"),
    [
        ("river", {"mention_enabled": False}, {"mention_enabled": True}),
        ("RIVER", {}, {"mention_case_sensitive": False}),
        ("river", {"mention_exclusions": ["river bend"]}, {"mention_exclusions": []}),
    ],
)
async def test_new_match_competition_seals_previously_unique_agent_result(
    client, fake_llm, competitor_alias, previous, next_values,
):
    project, base, url = await setup_agent(client)
    project_url = f"/api/v1/projects/{project['id']}"
    marker = "previously-unique-character-background"
    original = await _character(client, project_url, "本名甲", aliases=["river"], background=marker)
    competitor = await _character(client, project_url, "本名乙", aliases=[competitor_alias])
    await _set_visibility(client, project_url, competitor["id"], **previous)

    text = "查询 river bend 的档案"
    fake_llm.push_tools([call("get_character", {"id": original["id"]}, "unique")])
    fake_llm.push_tools([("text", {"text": "已读取"})])
    assert (await wait_task(client, await send(client, url, text)))["status"] == "succeeded"
    assert _outgoing_results(fake_llm)["unique"]["background"] == marker

    await _set_visibility(client, project_url, competitor["id"], **next_values)
    old = (await client.get(url)).json()
    assert marker in json.dumps(old["messages"], ensure_ascii=False)
    assert old["history_sealed"]
    rejected = await client.post(url + "/messages", json={"content": text})
    assert rejected.status_code == 409 and rejected.json()["code"] == "ai_history_sealed", rejected.text
    assert len(fake_llm.tool_calls) == 2

    created = await client.post(base + "/conversations", json={})
    assert created.status_code == 201, created.text
    new_url = base + f"/conversations/{created.json()['id']}"
    fake_llm.push_tools([call("get_character", {"id": original["id"]}, "ambiguous")])
    fake_llm.push_tools([("text", {"text": "请明确对象"})])
    assert (await wait_task(client, await send(client, new_url, text)))["status"] == "succeeded"
    assert _outgoing_results(fake_llm)["ambiguous"] == {"error": "不存在"}
    assert marker not in json.dumps(fake_llm.tool_calls[-2:], ensure_ascii=False)


async def test_agent_analysis_filters_character_refs_inside_confirmed_foreshadow(client, app, fake_llm):
    project, _, url = await setup_agent(client)
    pid = project["id"]
    project_url = f"/api/v1/projects/{pid}"
    forbidden = await _character(client, project_url, "分析不可见")
    visible = await _character(client, project_url, "分析可见")
    await _set_visibility(client, project_url, forbidden["id"], ai_mode="never")
    chapter = await client.post(project_url + "/chapters", json={"chapter_no": 1, "title": "首章"})
    assert chapter.status_code == 201, chapter.text
    chapter_id = chapter.json()["id"]
    async with app.state.sessionmaker() as db:
        analysis = ChapterAnalysis(project_id=pid, chapter_id=chapter_id, chapter_no=1, content_hash="0" * 64)
        db.add(analysis)
        await db.flush()
        db.add(AnalysisChange(
            analysis_id=analysis.id, project_id=pid, chapter_no=1, kind="foreshadow_new", status="applied",
            target={"title": "回写伏笔"}, after={"character_ids": [forbidden["id"], visible["id"]]},
        ))
        await db.commit()
    fake_llm.push_tools([call("get_chapter_analysis", {"chapter_id": chapter_id}, "analysis")])
    fake_llm.push_tools([("text", {"text": "完成"})])
    task_id = await send(client, url, "请看分析可见在这一章的回写")
    assert (await wait_task(client, task_id))["status"] == "succeeded"
    changes = _outgoing_results(fake_llm)["analysis"]["analysis"]["changes"]
    assert changes[0]["after"]["character_ids"] == [visible["id"]]
