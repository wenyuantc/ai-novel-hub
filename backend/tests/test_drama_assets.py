import pytest

from app.models import Character, WorldSetting
from app.plugins.drama.models import DramaEpisode, DramaSeries
from app.plugins.drama.services.assets import merge_extracted
from tests.helpers import create_project, register

BASE = "/api/v1/plugins/drama"


@pytest.fixture
async def library(app, client, system_admin):
    await register(client)
    project = await create_project(client)
    assert (await system_admin.patch("/api/v1/admin/plugins/drama", json={"enabled": True, "revision": 0})).is_success
    async with app.state.sessionmaker() as db:
        series = DramaSeries(project_id=project["id"], title="短剧")
        db.add(series)
        await db.flush()
        episode = DramaEpisode(series_id=series.id, title="第一集", number=1)
        db.add(episode)
        await db.commit()
        return series.id, episode.id, project["id"]


async def test_asset_edit_default_and_membership(client, library):
    sid, eid, _ = library
    library_url = f"{BASE}/series/{sid}"
    character = (await client.post(library_url + "/characters", json={"name": "甲"})).json()
    assert character["looks"][0]["is_default"]
    first = character["looks"][0]
    initial = f"{BASE}/looks/{first['id']}"
    assert (await client.delete(initial, params={"revision": first["revision"]})).status_code == 400
    url = f"{BASE}/characters/{character['id']}/looks"
    second = (await client.post(url, json={"name": "礼服", "is_default": True})).json()
    assert second["is_default"]
    assert (await client.post(url, json={"name": "礼服"})).status_code == 409
    assert (await client.patch(initial, json={"name": "便服", "revision": first["revision"]})).status_code == 409
    bundle = (await client.get(library_url + "/assets")).json()
    looks = bundle["characters"][0]["looks"]
    assert sum(row["is_default"] for row in looks) == 1
    join = f"{BASE}/episodes/{eid}/assets/looks/{second['id']}"
    assert (await client.put(join, params={"revision": 0})).status_code == 204
    assert (await client.delete(join, params={"revision": 0})).status_code == 409
    episode_assets = (await client.get(f"{BASE}/episodes/{eid}/assets")).json()
    assert [row["id"] for row in episode_assets["characters"][0]["looks"]] == [second["id"]]
    assert (await client.delete(join, params={"revision": 1})).status_code == 204
    assert not (await client.get(f"{BASE}/episodes/{eid}/assets")).json()["characters"]


async def test_extraction_links_only_same_project_survives_novel_delete(app, client, library):
    sid, eid, pid = library
    other = await create_project(client, title="另一部")
    async with app.state.sessionmaker() as db:
        novel = Character(project_id=pid, name="白云", aliases=["云儿"], appearance="红发", role_type="主角")
        place = WorldSetting(project_id=pid, title="客栈", content="灰墙", category="place")
        outside = WorldSetting(project_id=other["id"], title="另一处", category="place")
        db.add_all([novel, place, outside])
        await db.flush()
        payload = {
            "characters": [{"name": "云儿", "looks": [{"name": "常服"}]}],
            "scenes": [{"location": "客栈", "time_of_day": "夜"}],
            "props": [{"name": "杯子"}],
        }
        series = await db.get(DramaSeries, sid)
        episode = await db.get(DramaEpisode, eid)
        for _ in range(2):
            await merge_extracted(db, series, episode, payload)
        await db.commit()
        nid, place_id, outside_id = novel.id, place.id, outside.id

    assets_url = f"{BASE}/episodes/{eid}/assets"
    bundle = (await client.get(assets_url)).json()
    (character,) = bundle["characters"]
    (scene,) = bundle["scenes"]
    assert len(bundle["props"]) == len(character["looks"]) == 1
    assert character["novel_character_id"] == nid and character["appearance"] == "红发"
    assert scene["novel_world_setting_id"] == place_id and scene["description"] == "灰墙"
    scene_url = f"{BASE}/scenes/{scene['id']}"
    assert (
        await client.patch(
            scene_url,
            json={
                "revision": scene["revision"],
                "novel_world_setting_id": outside_id,
            },
        )
    ).status_code == 404
    assert (
        await client.patch(
            f"{BASE}/characters/{character['id']}",
            json={
                "revision": character["revision"],
                "name": "改名",
            },
        )
    ).is_success
    async with app.state.sessionmaker() as db:
        novel = await db.get(Character, nid)
        assert novel.name == "白云"
        await db.delete(novel)
        await db.commit()
    (saved,) = (await client.get(assets_url)).json()["characters"]
    assert saved["name"] == "改名" and saved["novel_character_id"] is None and saved["appearance"] == "红发"


async def test_create_and_join_is_atomic_and_scoped(app, client, library):
    sid, eid, pid = library
    series = f"{BASE}/series/{sid}"
    collection = series + "/props"
    params = {"episode_id": eid, "episode_revision": 0}
    response = await client.post(collection, params=params, json={"name": "茶杯", "description": "杯沿有缺口"})
    assert response.status_code == 201, response.text
    prop = response.json()
    assert (await client.post(collection, params=params, json={"name": "过期道具"})).status_code == 409
    assert (await client.post(collection, params={"episode_id": eid}, json={"name": "遗漏修订"})).status_code == 400
    episode_url = f"{BASE}/episodes/{eid}/assets"
    assert [p["id"] for p in (await client.get(episode_url)).json()["props"]] == [prop["id"]]
    assert len((await client.get(series + "/assets")).json()["props"]) == 1
    async with app.state.sessionmaker() as db:
        sibling = DramaSeries(project_id=pid, title="同书另一剧")
        db.add(sibling)
        await db.commit()
        sibling_id = sibling.id
    other = (await client.post(f"{BASE}/series/{sibling_id}/props", json={"name": "袖箭"})).json()
    response = await client.put(f"{episode_url}/props/{other['id']}", params={"revision": 1})
    assert response.status_code == 404
    response = await client.delete(f"{BASE}/props/{prop['id']}", params={"revision": prop["revision"]})
    assert response.status_code == 204
    assert not (await client.get(episode_url)).json()["props"]


async def test_asset_writes_require_project_access(client, make_client, library):
    sid, _, _ = library
    url = f"{BASE}/series/{sid}"
    scene = (await client.post(url + "/scenes", json={"location": "林中", "time_of_day": "傍晚"})).json()
    outsider = make_client()
    await register(outsider, "无关用户")
    assert (await outsider.get(url + "/assets")).status_code == 404
    assert (await outsider.post(url + "/characters", json={"name": "甲"})).status_code == 404
    scene_url = f"{BASE}/scenes/{scene['id']}"
    assert (await outsider.patch(scene_url, json={"revision": 0, "location": "远方"})).status_code == 404
    assert (await outsider.delete(scene_url, params={"revision": 0})).status_code == 404
    assert (await client.patch(scene_url, json={"revision": 0})).status_code == 422
    assert (await client.patch(scene_url, json={"revision": 0, "location": None})).status_code == 422
    saved = (await client.patch(scene_url, json={"revision": 0, "image_prompt": "雾气弥漫"})).json()
    assert saved["revision"] == 1 and saved["image_prompt"] == "雾气弥漫"


async def test_manual_link_relink_and_ambiguous_extraction(app, client, library):
    sid, eid, pid = library
    async with app.state.sessionmaker() as db:
        first = Character(project_id=pid, name="杜一", aliases=["阿杜"], appearance="蓝发")
        second = Character(project_id=pid, name="杜二", aliases=["阿杜"], appearance="紫发")
        db.add_all([first, second])
        await db.flush()
        episode = await db.get(DramaEpisode, eid)
        series = await db.get(DramaSeries, sid)
        await merge_extracted(
            db, series, episode, {"characters": [{"name": "阿杜（掌柜）", "looks": [{"name": "旧衣"}]}]}
        )
        await merge_extracted(db, series, episode, {"characters": [{"name": "阿杜", "looks": [{"name": "旧 衣"}]}]})
        await db.commit()
        first_id, second_id = first.id, second.id
    (char,) = (await client.get(f"{BASE}/episodes/{eid}/assets")).json()["characters"]
    assert char["novel_character_id"] is None and len(char["looks"]) == 1
    url = f"{BASE}/characters/{char['id']}"
    for target, expected_name in ((first_id, "杜一"), (second_id, "杜二"), (None, None)):
        response = await client.patch(url, json={"revision": char["revision"], "novel_character_id": target})
        assert response.status_code == 200, response.text
        char = response.json()
        assert char["novel_character_id"] == target and char["novel_character_name"] == expected_name
        assert char["appearance"] == "蓝发"
