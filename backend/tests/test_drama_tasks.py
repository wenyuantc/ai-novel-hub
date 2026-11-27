"""短剧 AI 任务通过正式的模型、授权及任务入口运行。"""

import pytest

from app.plugins.drama.services.task_material import script_batches


def test_storyboard_batches_keep_every_scene_and_split_oversized_scenes():
    scenes = [f"## S{i} | 外景 · 山门 | 日\n" + "雪" * 50 + "\n" for i in range(1, 8)]
    batches = script_batches("".join(scenes), 160)
    assert "".join(batches) == "".join(scenes)
    assert all(0 < len(batch) <= 160 for batch in batches)
    assert all(batch.startswith("## S") for batch in batches)
    assert "".join(script_batches("风" * 401, 80)) == "风" * 401


@pytest.mark.parametrize("bad", [[], [{"type": "look", "id": 1}] * 2])
def test_prompts_require_unique_targets(bad):
    from pydantic import ValidationError

    from app.plugins.drama.services.task_material import StartDramaTask

    with pytest.raises(ValidationError):
        StartDramaTask(kind="drama.prompts", targets=bad)


async def episode_with_model(client, admin):
    from tests.helpers import create_ai_config
    from tests.test_drama_episodes import BASE, setup_series

    _, series_id = await setup_series(client, admin)
    await create_ai_config(client)
    response = await client.post(
        f"{BASE}/series/{series_id}/episodes", json={"title": "夜", "source_text": "雪落在门前。"}
    )
    assert response.status_code == 201, response.text
    return f"{BASE}/episodes/{response.json()['id']}"


async def test_draft_confirmation_observes_edits_made_after_completion(client, system_admin, fake_llm):
    from tests.helpers import Gate, stream_task

    url = await episode_with_model(client, system_admin)
    wrong_actor = {"X-Editor-Actor-Id": "2147483647"}
    invalid = await client.post(url + "/tasks", json={"kind": "drama.rewrite"}, headers=wrong_actor)
    assert invalid.status_code == 409 and not fake_llm.calls
    gate = Gate()
    script = "## S1 | 外景 · 山门 | 夜\n雪落在门前。"
    fake_llm.push([gate, script])
    started = await client.post(url + "/tasks", json={"kind": "drama.rewrite"})
    assert started.status_code == 202, started.text
    try:
        await gate.wait_reached()
        assert (await client.post(url + "/tasks", json={"kind": "drama.rewrite"})).status_code == 409
        revision = (await client.get(url)).json()["revision"]
        edited = await client.patch(url, json={"revision": revision, "script": "手改剧本"})
        assert edited.is_success, edited.text
    finally:
        gate.release()
    task = started.json()
    events = await stream_task(client, task)
    assert events[-1][0] == "done", events[-1]
    assert events[-1][1]["result"]["applied"] is False
    assert (await client.get(url)).json()["script"] == "手改剧本"
    invalid = await client.post(
        f"{url}/tasks/{task['id']}/apply", json={"task_key": task["task_key"]}, headers=wrong_actor
    )
    assert invalid.status_code == 409
    latest = (await client.get(url)).json()
    second_edit = await client.patch(url, json={"revision": latest["revision"], "script": "二次手改"})
    assert second_edit.is_success, second_edit.text
    adopted = await client.post(f"{url}/tasks/{task['id']}/apply", json={"task_key": task["task_key"]})
    assert adopted.status_code == 409, adopted.text
    assert (await client.get(url)).json()["script"] == "二次手改"


async def test_storyboard_retries_second_batch_from_checkpoint(client, system_admin, fake_llm):
    import json

    from tests.helpers import wait_task

    url = await episode_with_model(client, system_admin)
    revision = (await client.get(url)).json()["revision"]
    script = "## S1 | 内景 · 书阁 | 夜\n" + "寻" * 3000 + "\n## S2 | 外景 · 前庭 | 日\n" + "风" * 3000
    edited = await client.patch(url, json={"revision": revision, "script": script})
    assert edited.is_success, edited.text

    def shot(title):
        return json.dumps(
            {"shots": [{"title": title, "description": "他在寻书", "shot_size": "中景", "duration_seconds": 5}]}
        )

    fake_llm.push(shot("夜"))
    fake_llm.push("无效 JSON")
    fake_llm.push("第二次仍无效")
    started = await client.post(url + "/tasks", json={"kind": "drama.storyboard"})
    assert started.status_code == 202, started.text
    failed = await wait_task(client, started.json(), within=10)
    assert (failed["status"], failed["step_cursor"]) == ("failed", 1), failed
    assert (await client.get(url + "/shots")).json() == []
    assert len(fake_llm.calls) == 3
    key = {"task_key": failed["task_key"]}
    output = await client.get(f"/api/v1/tasks/{failed['id']}/output", params=key)
    assert "第二次仍无效" in output.json()["text"]
    fake_llm.push(shot("昼"))
    retry = await client.post(f"/api/v1/tasks/{failed['id']}/retry", params=key)
    assert retry.status_code == 202, retry.text
    final = await wait_task(client, retry.json(), within=10)
    assert (final["status"], final["step_cursor"]) == ("succeeded", 2), final
    assert len(fake_llm.calls) == 4
    assert [row["title"] for row in (await client.get(url + "/shots")).json()] == ["夜", "昼"]


def test_published_successor_requires_same_signature_and_append_only_batches():
    from types import SimpleNamespace

    from app.plugins.drama.handlers import DramaHandler

    def state(cursor, drafts, signature="原始输入"):
        return SimpleNamespace(
            kind="drama.storyboard",
            step_cursor=cursor,
            checkpoint={
                "signature": signature,
                "drafts": drafts,
            },
        )

    first = state(1, [{"shots": ["开场"]}])
    assert DramaHandler.checkpoint_successor(first, state(2, [{"shots": ["开场"]}, {"shots": ["收束"]}]))
    assert not DramaHandler.checkpoint_successor(first, state(2, [{"shots": ["替换"]}, {"shots": ["收束"]}]))
    assert not DramaHandler.checkpoint_successor(first, state(2, [{"shots": ["开场"]}, {}], "新输入"))
    assert not DramaHandler.checkpoint_successor(first, first)


async def test_linked_appearance_obeys_visibility_on_real_model_dispatch(client, system_admin, fake_llm, app):
    from app.models import Character
    from app.plugins.drama.models import (
        DramaCharacter,
        DramaEpisode,
        DramaLook,
        DramaProp,
        DramaScene,
        DramaSeries,
        DramaShot,
        DramaShotLook,
        DramaShotProp,
    )
    from tests.helpers import run_task

    url = await episode_with_model(client, system_admin)
    async with app.state.sessionmaker() as db:
        episode = await db.get(DramaEpisode, int(url.rsplit("/", 1)[1]))
        series = await db.get(DramaSeries, episode.series_id)
        entry = Character(
            project_id=series.project_id,
            name="白衫客",
            ai_mode="always",
            appearance="绝密暗记",
            ai_hidden_builtin_fields=["appearance"],
        )
        db.add(entry)
        await db.flush()
        char = DramaCharacter(
            series_id=series.id, novel_character_id=entry.id, name=entry.name, appearance=entry.appearance
        )
        db.add(char)
        await db.flush()
        look = DramaLook(character_id=char.id, name="常服", styling="青色披风", is_default=True)
        db.add(look)
        await db.flush()
        scene = DramaScene(series_id=series.id, location="山门", time_of_day="夜")
        prop = DramaProp(series_id=series.id, name="纸伞")
        db.add_all([scene, prop])
        await db.flush()
        shot = DramaShot(episode_id=episode.id, scene_id=scene.id, position=10, title="入山")
        db.add(shot)
        await db.flush()
        db.add_all([DramaShotLook(shot_id=shot.id, look_id=look.id), DramaShotProp(shot_id=shot.id, prop_id=prop.id)])
        await db.commit()
        look_id, shot_id = look.id, shot.id
    import json

    invalid = await client.post(
        url + "/tasks",
        json={
            "kind": "drama.prompts",
            "targets": [
                {"type": "shot", "id": shot_id},
                {"type": "look", "id": look_id},
            ],
        },
    )
    assert invalid.status_code == 400 and not fake_llm.calls
    fake_llm.push(json.dumps({"prompts": [{"kind": "shot", "id": shot_id, "prompt": "推镜头"}]}))
    task, _ = await run_task(
        client,
        await client.post(
            url + "/tasks",
            json={
                "kind": "drama.prompts",
                "targets": [{"type": "shot", "id": shot_id}],
            },
        ),
    )
    assert task["status"] == "succeeded", task
    sent = fake_llm.calls[0]["messages"][-1]["content"]
    assert all(value in sent for value in ("青色披风", "山门", "纸伞")) and "绝密暗记" not in sent
