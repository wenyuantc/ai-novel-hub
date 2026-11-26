"""用作者实际使用的预览、移动、拆并和历史恢复入口核对故事日期。"""

import pytest

from tests.helpers import create_ai_config, run_task
from tests.test_batch_revision import chapter as create_chapter
from tests.test_scene_move import preview
from tests.test_scene_planning import scene_setup
from tests.test_scene_restructure import _body, _preview, _start


async def configure(client, base):
    url = f"{base}/story-timeline"
    response = await client.put(f"{url}/calendar", json={
        "expected_revision": 0, "era_name": "星历", "months": [{"id": "mist", "name": "雾月", "days": 20}],
    })
    assert response.is_success, response.text
    return url


def date_url(timeline, kind, item_id):
    return f"{timeline}/{kind}/{item_id}/date"


async def set_day(client, url, day):
    current = await client.get(url)
    assert current.is_success, current.text
    response = await client.put(url, json={
        "expected_revision": current.json()["revision"],
        "date": None if day is None else {"year": 3, "month_id": "mist", "day": day},
    })
    assert response.is_success, response.text


@pytest.mark.parametrize("explicit", [False, True])
async def test_move_keeps_override_or_changes_inherited_date_and_rejects_old_preview(client, explicit):
    _, base, source, scenes_url, scenes = await scene_setup(client)
    target = await create_chapter(client, base, 2, "目的章")
    timeline = await configure(client, base)
    scene_id = scenes[1]["id"]
    scene_date = date_url(timeline, "scene", scene_id)
    await set_day(client, date_url(timeline, "chapter", source["id"]), 10)
    target_date = date_url(timeline, "chapter", target["id"])
    await set_day(client, target_date, 12)
    if explicit:
        await set_day(client, scene_date, 3)
    launch, body = await preview(client, scenes_url, scene_id, target)
    assert launch["story_date_before"] == ("场景指定 · 星历3年雾月3日" if explicit else "沿用本章 · 星历3年雾月10日")
    assert launch["story_date_after"] == ("场景指定 · 星历3年雾月3日" if explicit else "沿用本章 · 星历3年雾月12日")
    await set_day(client, target_date, 13)
    move_url = f"{scenes_url}/{scene_id}/move"
    stale = await client.post(move_url, json=body)
    assert stale.status_code == 409 and stale.json()["code"] == "scene_move_preview_changed"
    _, body = await preview(client, scenes_url, scene_id, target)
    moved = await client.post(move_url, json=body)
    assert moved.is_success, moved.text
    date = (await client.get(scene_date)).json()
    assert (date["chapter_id"], date["source"], date["effective_date"]["day"]) == (
        target["id"], "scene" if explicit else "inherited", 3 if explicit else 13,
    )
    assert (await client.delete(f"{base}/chapters/{source['id']}")).status_code == 204
    assert (await client.get(date_url(timeline, "chapter", source["id"]))).status_code == 404
    assert (await client.get(date_url(timeline, "scene", scenes[0]["id"]))).status_code == 404
    retained = (await client.get(scene_date)).json()
    assert (retained["source"], retained["effective_date"]) == (date["source"], date["effective_date"])
    if explicit:
        await set_day(client, scene_date, None)
        cleared = (await client.get(scene_date)).json()
        assert cleared["source"] == "inherited" and cleared["effective_date"]["day"] == 13


async def test_split_merge_and_restore_preserve_date_source_and_retired_date_corrections(client):
    _, base, chapter, scenes_url, scenes = await scene_setup(client, content="山中出发", count=1)
    timeline = await configure(client, base)
    left_id = scenes[0]["id"]
    left_date = date_url(timeline, "scene", left_id)
    await set_day(client, date_url(timeline, "chapter", chapter["id"]), 10)
    await set_day(client, left_date, 10)
    split = await client.post(f"{scenes_url}/{left_id}/split", json={"expected_content_revision": 0, "offset": 2})
    assert split.is_success, split.text
    right_id = split.json()["items"][1]["id"]
    right_date = date_url(timeline, "scene", right_id)
    assert (await client.get(right_date)).json()["explicit_date"]["day"] == 10
    await set_day(client, right_date, None)
    args = {"scene_ids": [left_id, right_id], "expected_content_revision": split.json()["content_revision"]}
    review = (await client.post(f"{scenes_url}/merge-preview", json=args)).json()
    conflict = next(item for item in review["conflicts"] if item["field"] == "story_date")
    assert conflict == {
        "field": "story_date", "left": "场景指定 · 星历3年雾月10日", "right": "沿用本章 · 星历3年雾月10日",
    }
    merge = {**args, "expected_planning_revision": review["planning_revision"], "signature": review["signature"],
             "metadata_source": "right"}
    await set_day(client, left_date, 12)
    assert (await client.post(f"{scenes_url}/merge", json=merge)).status_code == 409
    merge["signature"] = (await client.post(f"{scenes_url}/merge-preview", json=args)).json()["signature"]
    versions_url = f"{base}/chapters/{chapter['id']}/versions"
    split_version = (await client.get(versions_url)).json()[0]["id"]
    merged = await client.post(f"{scenes_url}/merge", json=merge)
    assert merged.is_success, merged.text
    assert (await client.get(left_date)).json()["source"] == "inherited"
    assert (await client.get(right_date)).json()["active"] is False
    await set_day(client, right_date, 15)
    restore = await client.post(f"{versions_url}/{split_version}/restore", json={
        "expected_content_revision": merged.json()["content_revision"],
    })
    assert restore.is_success, restore.text
    current = (await client.get(right_date)).json()
    assert current["active"] and current["explicit_date"]["day"] == 15
    assert (await client.get(left_date)).json()["source"] == "inherited"


@pytest.mark.parametrize("operation", ["split", "merge"])
async def test_ai_adoption_uses_same_date_rules(client, fake_llm, operation):
    _, base, chapter, _, scenes = await scene_setup(client, content="从山里出发。\n抵达港口。")
    await create_ai_config(client)
    timeline = await configure(client, base)
    relevant = scenes if operation == "merge" else scenes[:1]
    await set_day(client, date_url(timeline, "scene", relevant[-1]["id"]), 8)
    url = f"{base}/chapters/{chapter['id']}/scene-restructure"
    args = _body(relevant, operation)
    launch = await _preview(client, url, args)
    fake_llm.push('{"proposals":[{"candidate_id":1,"reason":"两处的场景连接自然"}]}')
    task, _ = await run_task(client, await _start(client, url, args, launch))
    result_url = f"{url}/suggestions/{task['task_key']}"
    result = (await client.get(result_url)).json()
    choice = {"metadata_source": "right"} if operation == "merge" else {"right_title": "港口"}
    applied = await client.post(f"{result_url}/apply", json={
        "candidate_id": 1, "signature": result["signature"], **choice,
    })
    assert applied.is_success, applied.text
    for item in applied.json()["items"]:
        date = (await client.get(date_url(timeline, "scene", item["id"]))).json()
        assert date["explicit_date"]["day"] == 8
