"""使用 FakeLLM 捕获真实 messages，验证区块来源的开启与关闭。"""

import json

import pytest
from sqlalchemy import event

from app.models import Chapter, Project, Scene, SceneSection
from app.services import context_builder, rich_document_ai
from app.services.rich_document_ai import visible_sections
from tests.helpers import Gate, create_ai_config, parse_sse, wait_task
from tests.test_agent_runtime import call, send, setup_agent
from tests.test_rich_document_api import KEY, SECTION_AFTER, add_section, save_payload
from tests.test_scene_planning import scene_setup


async def _add_visibility_examples(client, endpoint):
    body = save_payload((await client.get(endpoint)).json())
    add_section(body, enabled=True)
    body["document"]["sections"][0].update(title="开启区块名称", content="开启区块内容")
    body["document"]["sections"].append({
        "key": SECTION_AFTER, "anchor": 1, "order": 1, "title": "关闭区块名称", "content": "关闭区块内容",
        "formatting": {"version": 1, "boundaries": [], "inline": [], "blocks": []},
        "include_in_ai": False,
    })
    body["add_section_keys"].append(SECTION_AFTER)
    result = await client.put(endpoint, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["document"]["sections"][0]["key"] == KEY


@pytest.mark.parametrize("mode", ["generate", "polish"])
async def test_real_generation_messages_include_enabled_target_only(client, fake_llm, mode):
    _project, _base, _chapter, scenes_url, scenes = await scene_setup(client, content="正式正文", count=1)
    await create_ai_config(client)
    await _add_visibility_examples(client, f"{scenes_url}/{scenes[0]['id']}/document")
    revisions = (await client.get(scenes_url)).json()
    gate = Gate()
    fake_llm.push(["候选", gate, "正文"])
    started = await client.post(f"{scenes_url}/{scenes[0]['id']}/{mode}", json={
        "expected_content_revision": revisions["content_revision"],
        "expected_planning_revision": revisions["planning_revision"],
    })
    assert started.status_code == 202, started.text
    try:
        await gate.wait_reached()
        sent = str(fake_llm.calls[-1]["messages"])
        if mode == "polish":
            assert "正式正文" in sent
        assert "开启区块名称" in sent and "开启区块内容" in sent
        assert "关闭区块名称" not in sent and "关闭区块内容" not in sent
    finally:
        await client.post(f"/api/v1/tasks/{started.json()['id']}/cancel")
        gate.release()
        await wait_task(client, started.json()["id"])


async def test_real_generation_messages_include_enabled_prior_chapter_only(client, fake_llm):
    project, base, _chapter, scenes_url, scenes = await scene_setup(client, content="前章正文", count=1)
    await create_ai_config(client)
    await _add_visibility_examples(client, f"{scenes_url}/{scenes[0]['id']}/document")
    second = await client.post(f"{base}/chapters", json={"title": "第二章", "content": "当前正文"})
    assert second.status_code == 201, second.text
    url = f"/api/v1/projects/{project['id']}/chapters/{second.json()['id']}/scenes"
    page = (await client.get(url)).json()
    gate = Gate()
    fake_llm.push(["候选", gate, "正文"])
    started = await client.post(f"{url}/{page['items'][0]['id']}/generate", json={
        "expected_content_revision": page["content_revision"], "expected_planning_revision": page["planning_revision"],
    })
    assert started.status_code == 202, started.text
    try:
        await gate.wait_reached()
        sent = str(fake_llm.calls[-1]["messages"])
        assert "前章正文" in sent and "开启区块名称" in sent and "开启区块内容" in sent
        assert "关闭区块名称" not in sent and "关闭区块内容" not in sent
    finally:
        await client.post(f"/api/v1/tasks/{started.json()['id']}/cancel")
        gate.release()
        await wait_task(client, started.json()["id"])


async def test_real_generation_messages_include_enabled_previous_scene_without_future_sources(client, fake_llm):
    _project, _base, chapter, scenes_url, scenes = await scene_setup(client, content="第一景", count=2)
    await create_ai_config(client)
    await _add_visibility_examples(client, f"{scenes_url}/{scenes[0]['id']}/document")
    second_endpoint = f"{scenes_url}/{scenes[1]['id']}/document"
    future = save_payload((await client.get(second_endpoint)).json())
    add_section(future, enabled=True)
    future["document"]["sections"][0].update(title="未来区块名称", content="未来区块内容")
    assert (await client.put(second_endpoint, json=future)).status_code == 200
    revisions = (await client.get(scenes_url)).json()
    gate = Gate()
    fake_llm.push("第一景新提案")
    fake_llm.push(["第二景", gate, "续文"])
    started = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={
        "target_words": 900, "expected_content_revision": revisions["content_revision"],
        "expected_planning_revision": revisions["planning_revision"],
    })
    assert started.status_code == 202, started.text
    try:
        await gate.wait_reached()
        sent = str(fake_llm.calls[-1]["messages"])
        first_sent = str(fake_llm.calls[0]["messages"])
        assert "第一景新提案" in sent
        assert "开启区块名称" in sent and "开启区块内容" in sent
        assert "未来区块名称" in sent and "未来区块内容" in sent
        assert "未来区块名称" not in first_sent and "未来区块内容" not in first_sent
        assert "关闭区块名称" not in sent and "关闭区块内容" not in sent
    finally:
        await client.post(f"/api/v1/tasks/{started.json()['id']}/cancel")
        gate.release()
        await wait_task(client, started.json()["id"])


async def test_sqlite_nul_and_crlf_truncation_reaches_actual_messages_safely(app, client, fake_llm):
    project, _base, _chapter, scenes_url, scenes = await scene_setup(client, content="正文", count=1)
    await create_ai_config(client)
    endpoint = f"{scenes_url}/{scenes[0]['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body, enabled=True)
    body["document"]["sections"][0]["content"] = "\x00" + "a" * 1_998 + "\r\nx"
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    async with app.state.sessionmaker() as db:
        sections = await visible_sections(db, project["id"], scene_ids=[scenes[0]["id"]])
    assert sections[0].content == "\x00" + "a" * 1_998 and sections[0].truncated is True
    revisions = (await client.get(scenes_url)).json()
    gate = Gate()
    fake_llm.push(["候选", gate, "正文"])
    started = await client.post(f"{scenes_url}/{scenes[0]['id']}/generate", json={
        "expected_content_revision": revisions["content_revision"],
        "expected_planning_revision": revisions["planning_revision"],
    })
    assert started.status_code == 202, started.text
    try:
        await gate.wait_reached()
        sent = str(fake_llm.calls[-1]["messages"])
        assert "a" * 1_000 in sent and "已截断" in sent
    finally:
        await client.post(f"/api/v1/tasks/{started.json()['id']}/cancel")
        gate.release()
        await wait_task(client, started.json()["id"])


async def test_long_book_sections_are_bounded_before_materializing_context(app, client, monkeypatch):
    project, _base, _chapter, _scenes_url, _scenes = await scene_setup(client, content="正文", count=1)
    async with app.state.sessionmaker() as db:
        project_row = await db.get(Project, project["id"])
        for i in range(2, 52):
            chapter = Chapter(project_id=project["id"], chapter_no=i, title=f"第{i}章", content="正文")
            db.add(chapter)
            await db.flush()
            scene = Scene(chapter_id=chapter.id, content="正文", title="", summary="", sort=0,
                          separator_before="", active=True)
            db.add(scene)
            await db.flush()
            db.add(SceneSection(scene_id=scene.id, logical_key=f"00000000-0000-4000-8000-{i:012x}",
                                anchor=1, sort=0, title=f"作者资料{i}", content="甲" * 10_000,
                                include_in_ai=True))
        await db.commit()
        target = chapter
        seen, statements = [], []
        original = rich_document_ai.visible_sections

        async def observe(*args, **kwargs):
            values = await original(*args, **kwargs)
            seen.append((kwargs["max_rows"], kwargs["text_limit"], values))
            return values

        def record_sql(_conn, _cursor, statement, _params, _context, _many):
            if "from scene_sections" in statement.lower():
                statements.append(statement.lower())

        monkeypatch.setattr(rich_document_ai, "visible_sections", observe)
        event.listen(app.state.engine.sync_engine, "before_cursor_execute", record_sql)
        try:
            prior = await context_builder.prior_chapters(db, project_row, target, budget=1_024)
        finally:
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", record_sql)
    assert len(seen) == 1
    max_rows, text_limit, values = seen[0]
    assert max_rows == 1 and text_limit == 256 and len(values) <= 1
    assert sum(len(item.content) for item in values) <= 256
    assert any("limit" in statement and "substr" not in statement for statement in statements)
    assert any("limit" in statement and "substr" in statement for statement in statements)
    assert "作者资料50" in prior[-1].content and "作者资料2" not in prior[1].content


async def test_agent_real_tool_messages_keep_formal_offsets_and_filter_section_sources(client, fake_llm):
    project, _base, conversation = await setup_agent(client)
    base = f"/api/v1/projects/{project['id']}"
    formal = "甲" * 4_000 + "乙"
    chapter = await client.post(f"{base}/chapters", json={"title": "章节", "content": formal})
    assert chapter.status_code == 201, chapter.text
    chapter_id = chapter.json()["id"]
    scenes_url = f"{base}/chapters/{chapter_id}/scenes"
    scene = (await client.get(scenes_url)).json()["items"][0]
    await _add_visibility_examples(client, f"{scenes_url}/{scene['id']}/document")
    fake_llm.push_tools([call("read_chapter", {"id": chapter_id, "page": 1}, "chapter")])
    fake_llm.push_tools([("text", {"text": "已读取"})])
    task_id = await send(client, conversation, "请读取章节")
    assert (await wait_task(client, task_id))["status"] == "succeeded"
    messages = fake_llm.tool_calls[-1]["messages"]
    tool_result = next(message for message in messages if message.get("tool_call_id") == "chapter")
    data = json.loads(tool_result["content"])["data"]
    assert data["content"] == formal[:4_000] and data["pages"] == 2
    assert "开启区块名称" in data["author_sections"] and "开启区块内容" in data["author_sections"]
    assert "关闭区块名称" not in str(messages) and "关闭区块内容" not in str(messages)


async def test_agent_chapter_paging_limits_sections_by_exact_scene_projection(client, fake_llm):
    project, _base, conversation = await setup_agent(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "分页", "content": "甲" * 4_000})).json()
    scenes_url = f"{base}/chapters/{chapter['id']}/scenes"
    first = (await client.get(scenes_url)).json()["items"][0]
    second_response = await client.post(scenes_url, json={"expected_content_revision": 0, "content": "乙🙂尾"})
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()
    first_url = f"{scenes_url}/{first['id']}/document"
    first_body = save_payload((await client.get(first_url)).json())
    add_section(first_body, enabled=True)
    first_body["document"]["sections"][0].update(anchor=3_999, title="本页区块", content="本页资料")
    first_body["document"]["sections"].append({
        "key": SECTION_AFTER, "anchor": 4_000, "order": 0, "title": "边界区块", "content": "边界资料",
        "formatting": {"version": 1, "boundaries": [], "inline": [], "blocks": []}, "include_in_ai": True,
    })
    first_body["add_section_keys"].append(SECTION_AFTER)
    assert (await client.put(first_url, json=first_body)).status_code == 200
    second_url = f"{scenes_url}/{second['id']}/document"
    second_body = save_payload((await client.get(second_url)).json())
    add_section(second_body, enabled=True)
    second_body["document"]["sections"][0].update(anchor=0, title="下一景区块", content="下一景资料")
    second_body["document"]["sections"].append({
        "key": SECTION_AFTER, "anchor": 3, "order": 0, "title": "章末区块", "content": "章末资料",
        "formatting": {"version": 1, "boundaries": [], "inline": [], "blocks": []}, "include_in_ai": True,
    })
    second_body["add_section_keys"].append(SECTION_AFTER)
    assert (await client.put(second_url, json=second_body)).status_code == 200
    fake_llm.push_tools([
        call("read_chapter", {"id": chapter["id"], "page": 1}, "first"),
        call("read_chapter", {"id": chapter["id"], "page": 2}, "second"),
    ])
    fake_llm.push_tools([("text", {"text": "已读取"})])
    task_id = await send(client, conversation, "请分页读取")
    assert (await wait_task(client, task_id))["status"] == "succeeded"
    data = {message["tool_call_id"]: json.loads(message["content"])["data"]
            for message in fake_llm.tool_calls[-1]["messages"] if message["role"] == "tool"}
    assert data["first"]["content"] == "甲" * 4_000
    assert "本页区块" in data["first"]["author_sections"]
    assert all(name not in data["first"]["author_sections"] for name in ("边界区块", "下一景区块", "章末区块"))
    assert data["second"]["content"] == "\n\n乙🙂尾"
    assert all(name in data["second"]["author_sections"] for name in ("边界区块", "下一景区块", "章末区块"))
    assert "本页区块" not in data["second"]["author_sections"]


async def test_agent_empty_chapter_page_includes_section_at_zero(client, fake_llm):
    project, _base, conversation = await setup_agent(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "空章", "content": ""})).json()
    scenes_url = f"{base}/chapters/{chapter['id']}/scenes"
    scene = (await client.get(scenes_url)).json()["items"][0]
    endpoint = f"{scenes_url}/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body, enabled=True)
    body["document"]["sections"][0]["anchor"] = 0
    assert (await client.put(endpoint, json=body)).status_code == 200
    fake_llm.push_tools([call("read_chapter", {"id": chapter["id"], "page": 1}, "empty")])
    fake_llm.push_tools([("text", {"text": "已读取"})])
    task_id = await send(client, conversation, "读取空章")
    assert (await wait_task(client, task_id))["status"] == "succeeded"
    message = next(row for row in fake_llm.tool_calls[-1]["messages"] if row.get("tool_call_id") == "empty")
    data = json.loads(message["content"])["data"]
    assert data["content"] == "" and data["pages"] == 1 and "作者🙂" in data["author_sections"]


async def test_rewrite_preview_real_messages_include_allowed_scene_material_only(client, fake_llm):
    _project, _base, _chapter, scenes_url, scenes = await scene_setup(client, content="第一景", count=2)
    await create_ai_config(client)
    await _add_visibility_examples(client, f"{scenes_url}/{scenes[0]['id']}/document")
    current_url = f"{scenes_url}/{scenes[1]['id']}/document"
    body = save_payload((await client.get(current_url)).json())
    add_section(body, enabled=True)
    body["document"]["sections"][0].update(title="当前开放名称", content="当前开放内容")
    assert (await client.put(current_url, json=body)).status_code == 200
    page = (await client.get(scenes_url)).json()
    fake_llm.push("润色候选")
    response = await client.post(f"{scenes_url}/{scenes[1]['id']}/rewrite", json={
        "op": "rewrite", "start": 0, "end": 2,
        "expected_content_revision": page["content_revision"],
        "expected_planning_revision": page["planning_revision"],
    })
    assert response.status_code == 200, response.text
    event, result = parse_sse(response.text)[-1]
    assert event == "done" and result["original"] == "第二" and result["text"] == "润色候选"
    sent = str(fake_llm.calls[-1]["messages"])
    assert "开启区块名称" in sent and "开启区块内容" in sent
    assert "当前开放名称" in sent and "当前开放内容" in sent
    assert "关闭区块名称" not in sent and "关闭区块内容" not in sent
    assert (await client.get(current_url)).json()["document"]["content"] == "第二段"
