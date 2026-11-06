"""真实生成消息必须使用章节位置对应的设定，不泄漏未来变化。"""

from tests.helpers import analysis_json, create_ai_config, create_project, register, run_task


async def _seed_history(client, *, first_content=""):
    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapters = {}
    for number in (5, 20, 21):
        response = await client.post(
            f"{base}/chapters",
            json={"chapter_no": number, "title": f"第{number}章", "content": first_content if number == 5 else ""},
        )
        assert response.status_code == 201, response.text
        chapters[number] = response.json()
    response = await client.post(
        f"{base}/characters", json={"name": "林远", "role_type": "主角", "current_state": "初始境界"}
    )
    assert response.status_code == 201, response.text
    character_id = response.json()["id"]
    policy_url = f"{base}/setting-entries/character/{character_id}/ai-settings"
    policy = (await client.get(policy_url)).json()
    updated = await client.patch(policy_url, json={
        "expected_ai_visibility_revision": policy["ai_visibility_revision"], "ai_mode": "always",
    })
    assert updated.status_code == 200, updated.text
    response = await client.get(
        f"{base}/setting-timelines", params={"subject_kind": "character", "character_id": character_id}
    )
    assert response.status_code == 200, response.text
    timeline = response.json()["items"][0]
    response = await client.post(
        f"{base}/setting-timelines/{timeline['id']}/progressions",
        json={
            "expected_revision": timeline["revision"],
            "field_key": "current_state",
            "operation": "replace",
            "value": "晋升后境界",
            "anchor_kind": "chapter",
            "chapter_id": chapters[20]["id"],
            "boundary": "end",
        },
    )
    assert response.status_code == 201, response.text
    return base, chapters


def _message_text(call):
    return "\n".join(message["content"] for message in call["messages"])


async def test_auto_continuation_keeps_target_chapter_setting_in_real_messages(client, fake_llm):
    _, chapters = await _seed_history(client)
    fake_llm.push("起" * 80)
    fake_llm.push("续" * 300)
    task, events = await run_task(
        client,
        await client.post(
            f"/api/v1/chapters/{chapters[5]['id']}/generate",
            json={"target_words": 300, "auto_continue": True},
        ),
    )
    assert task["status"] == "succeeded"
    assert len([event for event, _ in events if event == "continue"]) == 1
    assert len(fake_llm.calls) == 2
    for call in fake_llm.calls:
        prompt = _message_text(call)
        assert "当前状态：初始境界" in prompt
        assert "晋升后境界" not in prompt


async def test_batch_generation_uses_each_chapters_start_state_in_real_messages(client, fake_llm):
    base, chapters = await _seed_history(client)
    for text in ("前章正文", "本章正文", "后章正文"):
        fake_llm.push(text)
    task, _ = await run_task(
        client,
        await client.post(
            f"{base}/batch-generate",
            json={
                "start_chapter_id": chapters[5]["id"],
                "end_chapter_id": chapters[21]["id"],
                "auto_continue": False,
            },
        ),
    )
    assert task["status"] == "succeeded"
    assert len(fake_llm.calls) == 3
    first, same_chapter, following = map(_message_text, fake_llm.calls)
    for prompt in (first, same_chapter):
        assert "当前状态：初始境界" in prompt
        assert "晋升后境界" not in prompt
    assert "当前状态：晋升后境界" in following
    assert "当前状态：初始境界" not in following


async def test_revise_uses_target_chapter_setting_in_real_messages(client, fake_llm):
    _, chapters = await _seed_history(client, first_content="林远来到山门。")
    chapter_id = chapters[5]["id"]
    fake_llm.push(analysis_json())
    analysis_task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{chapter_id}/analyze"))
    assert analysis_task["status"] == "succeeded"
    response = await client.get(f"/api/v1/chapters/{chapter_id}/analyses")
    assert response.status_code == 200, response.text
    analysis_id = response.json()[0]["id"]
    fake_llm.push("重写后的山门正文。")
    task, _ = await run_task(
        client,
        await client.post(
            f"/api/v1/chapters/{chapter_id}/revise",
            json={"analysis_id": analysis_id, "instruction": "加强环境描写"},
        ),
    )
    assert task["status"] == "succeeded"
    prompt = _message_text(fake_llm.calls[-1])
    assert "当前状态：初始境界" in prompt
    assert "晋升后境界" not in prompt
