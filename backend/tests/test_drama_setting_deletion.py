import json

import pytest

from app.plugins.drama.models import DramaCharacter, DramaLook, DramaScene, DramaSeries
from tests.helpers import create_ai_config, create_project, register, run_task
from tests.test_wizard_thumbnail_replacement import KINDS, _entity


@pytest.mark.parametrize("kind", ["characters", "world"])
@pytest.mark.parametrize("action", ["delete", "replace"])
@pytest.mark.parametrize("other_book", [False, True])
async def test_deleting_setting_preserves_drama_and_checks_project(app, client, fake_llm, kind, action, other_book):
    await register(client)
    if action == "replace":
        await create_ai_config(client)
    original = (await create_project(client))["id"]
    asset_book = (await create_project(client, title="另一作品"))["id"] if other_book else original
    subject = await _entity(client, original, kind)
    model, column = ((DramaCharacter, "novel_character_id") if kind == "characters"
                     else (DramaScene, "novel_world_setting_id"))
    text_key = "name" if kind == "characters" else "location"
    async with app.state.sessionmaker.begin() as db:
        series = DramaSeries(project_id=asset_book, title="已有短剧")
        db.add(series)
        await db.flush()
        asset = model(series_id=series.id, **{column: subject, text_key: "短剧保留的名字"})
        db.add(asset)
        await db.flush()
        asset_id = asset.id
        if kind == "characters":
            db.add(DramaLook(character_id=asset_id, name="常服", is_default=True))

    route = f"/api/v1/projects/{original}"
    if action == "delete":
        response = await client.delete(f"{route}/{kind}/{subject}")
    else:
        fake_llm.push(json.dumps(KINDS[kind][4], ensure_ascii=False))
        response = await client.post(f"{route}/wizard/{kind}")
    if other_book:
        assert response.status_code == 409, response.text
        assert response.json()["code"] == ("wizard_reference_invalid" if action == "replace"
                                           else "setting_reference_invalid")
        assert (await client.get(f"{route}/{kind}/{subject}")).status_code == 200
    elif action == "delete":
        assert response.status_code == 204, response.text
    else:
        task, _ = await run_task(client, response)
        assert task["status"] == "succeeded", task
    async with app.state.sessionmaker() as db:
        asset = await db.get(model, asset_id)
        assert getattr(asset, text_key) == "短剧保留的名字"
        assert getattr(asset, column) == (subject if other_book else None)


async def test_removing_series_projection_detaches_only_its_own_drama_asset(app, client):
    from tests.test_series_transfer import _join_books, _post

    await register(client)
    book = await create_project(client)
    other = await create_project(client, title="另一本")
    route, library = await _join_books(client, book)
    entry = await _post(client, f"{route}/entries", {
        "kind": "character", "data": {"name": "共享角色"}, "expected_revision": library["revision"],
    })
    uses = f"{route}/entries/{entry['item']['id']}/uses"
    used = await _post(client, uses, {"project_id": book["id"], "expected_revision": entry["revision"]})
    ref = used["ref"]["id"]
    async with app.state.sessionmaker.begin() as db:
        series = DramaSeries(title="关联短剧", project_id=other["id"])
        db.add(series)
        await db.flush()
        actor = DramaCharacter(series_id=series.id, name="剧中角色", novel_character_id=ref)
        db.add(actor)
        await db.flush()
        actor_id, series_id = actor.id, series.id
        db.add(DramaLook(character_id=actor.id, name="便服", is_default=True))

    url = f"{uses}/{book['id']}"
    params = {"expected_revision": used["revision"]}
    blocked = await client.delete(url, params=params)
    assert blocked.status_code == 409 and blocked.json()["code"] == "setting_reference_invalid"
    async with app.state.sessionmaker.begin() as db:
        assert (await db.get(DramaCharacter, actor_id)).novel_character_id == ref
        (await db.get(DramaSeries, series_id)).project_id = book["id"]
    done = await client.delete(url, params=params)
    assert done.status_code == 200, done.text
    async with app.state.sessionmaker() as db:
        actor = await db.get(DramaCharacter, actor_id)
        assert actor.name == "剧中角色" and actor.novel_character_id is None
