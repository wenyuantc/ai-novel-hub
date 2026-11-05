import asyncio

from tests.helpers import Gate, create_project, register, wait_task
from tests.test_agent_runtime import call, send, setup_agent


async def test_conversation_crud_and_project_preference(client):
    project, base, url = await setup_agent(client)
    assert (await client.patch(url, json={"title": "角色修改", "auto_execute": True})).status_code == 200
    second = (await client.post(base + "/conversations", json={})).json()
    assert second["auto_execute"] is True
    other_project = await create_project(client)
    other = (await client.post(f"/api/v1/projects/{other_project['id']}/agent/conversations", json={})).json()
    assert other["auto_execute"] is False
    assert len((await client.get(base + "/conversations")).json()) == 2
    assert (await client.delete(url)).status_code == 204
    assert (await client.get(url)).status_code == 404


async def test_busy_conversation_blocks_send_delete_resume_finish(client, fake_llm):
    _, base, url = await setup_agent(client)
    gate = Gate()
    fake_llm.push_tools([gate, call("create_character", {"name": "林远"})])
    task_id = await send(client, url)
    await gate.wait_reached()
    assert (await client.post(url + "/messages", json={"content": "再次发送"})).status_code == 409
    assert (await client.delete(url)).status_code == 409
    gate.release()
    await wait_task(client, task_id, statuses=("paused",))
    assert (await client.post(url + "/messages", json={"content": "再次发送"})).status_code == 409
    assert (await client.post(f"/api/v1/tasks/{task_id}/resume")).status_code == 409
    assert (await client.post(f"/api/v1/tasks/{task_id}/finish")).status_code == 409
    assert (await client.post(base + f"/turns/{task_id}/reject-all")).status_code == 200
    await wait_task(client, task_id)


async def test_confirmation_refreshes_stale_preview_then_undo_conflicts(client, fake_llm):
    project, base, url = await setup_agent(client)
    char_url = f"/api/v1/projects/{project['id']}/characters"
    character = (await client.post(char_url, json={"name": "林远", "personality": "热情"})).json()
    fake_llm.push_tools([call("update_character", {"id": character["id"], "personality": "冷静"})])
    task_id = await send(client, url)
    await wait_task(client, task_id, statuses=("paused",))
    change = (await client.get(url)).json()["changes"][0]
    assert (await client.patch(char_url + f"/{character['id']}", json={"personality": "焦虑"})).status_code == 200
    path = base + f"/changes/{change['id']}"
    response = await client.post(path + "/confirm")
    assert response.status_code == 409 and response.json()["code"] == "preview_changed", response.text
    assert "焦虑" in str(response.json()["change"]["preview"])
    response = await client.post(path + "/confirm")
    assert response.status_code == 200 and response.json()["status"] == "executed", response.text
    await wait_task(client, task_id)
    assert (await client.patch(char_url + f"/{character['id']}", json={"personality": "激动"})).status_code == 200
    response = await client.post(path + "/undo")
    assert response.status_code == 409 and response.json()["code"] == "undo_conflict", response.text
    assert (await client.get(char_url)).json()[0]["personality"] == "激动"


async def test_confirm_concurrently_applies_only_once_and_turn_undo(client, fake_llm):
    project, base, url = await setup_agent(client)
    fake_llm.push_tools([call("create_character", {"name": "林远"})])
    task_id = await send(client, url)
    await wait_task(client, task_id, statuses=("paused",))
    change = (await client.get(url)).json()["changes"][0]
    results = await asyncio.gather(*(client.post(base + f"/changes/{change['id']}/confirm") for _ in range(2)))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    await wait_task(client, task_id)
    char_url = f"/api/v1/projects/{project['id']}/characters"
    assert len((await client.get(char_url)).json()) == 1
    response = await client.post(base + f"/turns/{task_id}/undo")
    assert response.status_code == 200, response.text
    assert response.json()["changes"][0]["status"] == "undone"
    assert (await client.get(char_url)).json() == []
    assert "用户撤销了" in (await client.get(url)).json()["messages"][-1]["content"]


async def test_cross_user_every_agent_endpoint_is_not_found(client, make_client, fake_llm):
    _, base, url = await setup_agent(client)
    fake_llm.push_tools([call("create_character", {"name": "林远"})])
    task_id = await send(client, url)
    await wait_task(client, task_id, statuses=("paused",))
    change = (await client.get(url)).json()["changes"][0]
    stranger = make_client()
    await register(stranger, "stranger")
    checks = [
        ("get", base + "/conversations", None),
        ("post", base + "/conversations", {}),
        ("get", url, None),
        ("get", url + "/generation-logs", None),
        ("patch", url, {"title": "侵入"}),
        ("delete", url, None),
        ("post", url + "/messages", {"content": "侵入"}),
        *[("post", base + f"/changes/{change['id']}/{action}", None) for action in ("confirm", "reject", "undo")],
        *[("post", base + f"/turns/{task_id}/{action}", None) for action in ("confirm-all", "reject-all", "undo")],
    ]
    for method, path, body in checks:
        response = await stranger.request(method, path, json=body)
        assert response.status_code == 404, (path, response.text)


async def test_undo_turn_is_atomic_when_earlier_change_conflicts(client, fake_llm):
    project, base, url = await setup_agent(client, auto=True)
    fake_llm.push_tools([call("create_character", {"name": "甲"}, "a"), call("create_character", {"name": "乙"}, "b")])
    task_id = await send(client, url)
    task = await wait_task(client, task_id)
    assert task["status"] == "succeeded", task
    char_url = f"/api/v1/projects/{project['id']}/characters"
    rows = (await client.get(char_url)).json()
    first = next(c for c in rows if c["name"] == "甲")
    await client.patch(char_url + f"/{first['id']}", json={"personality": "手动修改"})
    response = await client.post(base + f"/turns/{task_id}/undo")
    assert response.status_code == 409, response.text
    assert len((await client.get(char_url)).json()) == 2
    assert all(c["status"] == "executed" for c in (await client.get(url)).json()["changes"])


async def test_text_edit_records_agent_version_and_undo_restore(client, fake_llm):
    project, base, url = await setup_agent(client)
    chapter = (
        await client.post(
            f"/api/v1/projects/{project['id']}/chapters",
            json={
                "title": "雨夜",
                "content": "林远推开门，走进雨夜。",
            },
        )
    ).json()
    fake_llm.push_tools(
        [
            call(
                "replace_chapter_text",
                {
                    "chapter_id": chapter["id"],
                    "original": "走进雨夜",
                    "replacement": "踏入夜色",
                },
            )
        ]
    )
    task_id = await send(client, url)
    await wait_task(client, task_id, statuses=("paused",))
    response = await client.post(base + f"/turns/{task_id}/confirm-all")
    assert response.status_code == 200, response.text
    assert response.json()["changes"][0]["status"] == "executed", response.text
    await wait_task(client, task_id)
    chapter_url = f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}"
    assert (await client.get(chapter_url)).json()["content"] == "林远推开门，踏入夜色。"
    versions = (await client.get(chapter_url + "/versions")).json()
    assert any(v["source"] == "agent" for v in versions), versions
    response = await client.post(base + f"/turns/{task_id}/undo")
    assert response.status_code == 200, response.text
    assert (await client.get(chapter_url)).json()["content"] == "林远推开门，走进雨夜。"
    versions = (await client.get(chapter_url + "/versions")).json()
    assert any(v["source"] == "restore" for v in versions), versions


async def test_start_task_is_confirmed_and_can_be_cancelled(client, fake_llm):
    project, base, url = await setup_agent(client, auto=True)
    chapter = (
        await client.post(
            f"/api/v1/projects/{project['id']}/chapters",
            json={
                "title": "雨夜",
                "content": "林远推开门，走进雨夜。",
            },
        )
    ).json()
    gate = Gate()
    fake_llm.push([gate])
    fake_llm.push_tools(
        [
            call("create_character", {"name": "混合轮角色"}, "create"),
            call("start_task", {"kind": "chapter.analyze", "params": {"chapter_id": chapter["id"]}}, "start"),
        ]
    )
    task_id = await send(client, url)
    task = await wait_task(client, task_id, statuses=("paused", "failed"))
    assert task["status"] == "paused", task
    response = await client.post(base + f"/turns/{task_id}/confirm-all")
    assert response.status_code == 200, response.text
    change = response.json()["changes"][-1]
    assert change["status"] == "executed", response.text
    child_id = change["result"]["task_id"]
    await gate.wait_reached()
    assert (await client.post(f"/api/v1/tasks/{child_id}/cancel")).status_code == 200
    assert (await wait_task(client, child_id))["status"] == "cancelled"
    await wait_task(client, task_id)
    assert (await client.post(base + f"/changes/{change['id']}/undo")).status_code == 409
    undone = await client.post(base + f"/turns/{task_id}/undo")
    assert undone.status_code == 200, undone.text
    assert len(undone.json()["changes"]) == 1 and undone.json()["changes"][0]["status"] == "undone"
    assert (await client.get(url)).json()["changes"][-1]["status"] == "executed"


async def test_agent_versions_and_success_error_logs_survive_backup(client, fake_llm):
    from app.ai.providers import AIProviderError
    from tests.test_project_transfer import _backup, _import

    project, base, url = await setup_agent(client)
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"content": "唯一正文"})).json()
    fake_llm.push_tools(
        [call("replace_chapter_text", {"chapter_id": chapter["id"], "original": "唯一", "replacement": "修改"})]
    )
    fake_llm.push_tools(AIProviderError("测试模型失败"))
    task_id = await send(client, url)
    await wait_task(client, task_id, statuses=("paused",))
    assert (await client.post(base + f"/turns/{task_id}/confirm-all")).status_code == 200
    assert (await wait_task(client, task_id))["status"] == "failed"
    backup = await _backup(client, project["id"], versions="1", logs="1")
    assert any(v["source"] == "agent" for v in backup["tables"]["chapter_versions"])
    imported = await _import(client, backup)
    copied = await _backup(client, imported["project"]["id"], versions="1", logs="1")
    assert copied["app_schema"] == "0033"
    assert {log["status"] for log in copied["tables"]["generation_logs"]} == {"success", "error"}
    assert all(log["kind"] == "agent.turn" for log in copied["tables"]["generation_logs"])


async def test_generation_logs_are_conversation_scoped_and_hide_native_metadata(client, fake_llm, app):
    from sqlalchemy import select

    from app.models import GenerationLog

    project, base, url = await setup_agent(client)
    event, payload = call("get_project_overview", {})
    payload["provider_metadata"] = {"provider": "anthropic", "content": [{"thinking": "私有思考"}]}
    fake_llm.push_tools([(event, payload), ("usage", {"input_tokens": 12, "output_tokens": 3})])
    fake_llm.push_tools([("text", {"text": "完成"}), ("usage", {"input_tokens": 20, "output_tokens": 4})])
    task_id = await send(client, url)
    assert (await wait_task(client, task_id))["status"] == "succeeded"
    response = await client.get(url + "/generation-logs")
    assert response.status_code == 200, response.text
    logs = response.json()
    assert len(logs) == 2
    assert [log["params"]["round"] for log in logs] == [2, 1]
    assert {log["task_id"] for log in logs} == {task_id}
    assert sum(log["prompt_tokens"] for log in logs) == 32
    assert sum(log["completion_tokens"] for log in logs) == 7
    assert "provider_metadata" not in response.text and "私有思考" not in response.text
    # API key 不进入会话详情、生成记录、任务载荷与输出。
    secret = "sk-test-abcdefgh1234"
    for path in (url, url + "/generation-logs", f"/api/v1/tasks/{task_id}", f"/api/v1/tasks/{task_id}/output"):
        assert secret not in (await client.get(path)).text, path
    async with app.state.sessionmaker() as db:
        original = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == task_id))
        assert "provider_metadata" in original.params["tool_calls"][0]
    other = (await client.post(base + "/conversations", json={})).json()
    assert (await client.get(base + f"/conversations/{other['id']}/generation-logs")).json() == []
    foreign_project = await create_project(client)
    foreign_url = url.replace(f"projects/{project['id']}/", f"projects/{foreign_project['id']}/")
    assert (await client.get(foreign_url + "/generation-logs")).status_code == 404


async def test_serialization_failure_rolls_back_write_and_keeps_proposal(client, fake_llm, monkeypatch):
    from sqlalchemy.exc import DBAPIError

    from app.services.agent.tools import get_tool

    project, base, url = await setup_agent(client)
    fake_llm.push_tools([call("create_character", {"name": "不能部分提交"})])
    task_id = await send(client, url)
    await wait_task(client, task_id, statuses=("paused",))
    change = (await client.get(url)).json()["changes"][0]
    tool = get_tool("create_character")
    execute = tool.execute

    class SerializationError(Exception):
        sqlstate = "40001"

    async def conflicting(ctx, args):
        await execute(ctx, args)
        raise DBAPIError("insert", {}, SerializationError("concurrent update"))

    monkeypatch.setattr(tool, "execute", conflicting)
    response = await client.post(base + f"/changes/{change['id']}/confirm")
    assert response.status_code == 409 and "同时被其他操作修改" in response.json()["detail"], response.text
    assert (await client.get(f"/api/v1/projects/{project['id']}/characters")).json() == []
    assert (await client.get(url)).json()["changes"][0]["status"] == "proposed"
