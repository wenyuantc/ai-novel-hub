"""连载工作台 HTTP 边界：月份、稀疏编辑、定稿与发布输入。"""

from uuid import uuid4

from tests.helpers import create_project, register


async def test_serialization_invalid_filters_and_month(client):
    await register(client)
    pid = (await create_project(client))["id"]
    base = f"/api/v1/projects/{pid}/serialization"
    for suffix in (
        "/overview?month=0000-12", "/overview?month=2026-13", "/overview?month=2026-1",
        "/chapters?limit=0", "/chapters?planned_date=0", "/days/0", "/days/2026-02-30",
        "/publications?chapter_id=0", "/publications/1/export?format=zip",
    ):
        response = await client.get(base + suffix)
        assert response.status_code == 422, (suffix, response.text)


async def test_serialization_rejects_revision_and_date_coercion(client):
    await register(client)
    pid = (await create_project(client))["id"]
    base = f"/api/v1/projects/{pid}/serialization"
    for body in (
        {"expected_revision": False, "daily_goal": 1},
        {"expected_revision": 1, "daily_goal": True},
        {"expected_revision": 0},
        {"expected_revision": 0, "daily_goal": 0},
    ):
        assert (await client.put(base + "/goal", json=body)).status_code == 422
    for body in (
        {}, {"stage": None}, {"stage": "final"}, {"planned_date": 0},
        {"planned_date": "2026-11-18T00:00:00"}, {"planned_date": "2026-02-30"}, {"revision": 1},
    ):
        result = await client.patch(base + "/chapters/1", json={"expected_revision": 0, "expected_scene_id": 1, **body})
        assert result.status_code == 422, (body, result.text)
    url = base + "/chapters/1"
    for provided in ({}, {"expected_scene_id": True}, {"expected_scene_id": 0}):
        patch = await client.patch(url, json={"planned_date": None, "expected_revision": 0, **provided})
        assert patch.status_code == 422, patch.text
        finalize = await client.post(url + "/editions", json={
            "expected_revision": 0, "expected_content_revision": 0, "expected_fingerprint": "a" * 64, **provided,
        })
        assert finalize.status_code == 422, finalize.text
    url += "/publications"
    valid = {"expected_revision": 0, "edition_id": 1, "channel": "站点", "request_id": str(uuid4()),
             "published_at": "2026-11-01T00:00:00+08:00"}
    for changes in (
        {"edition_id": True}, {"published_at": "2026-11-01T00:00:00"}, {"published_at": "0001-01-01T00:00:00+08:00"},
        {"published_at": 1760000000}, {"channel": "  "}, {"channel": "a\u0000b"}, {"request_id": "broken"},
    ):
        result = await client.post(url, json={**valid, **changes})
        assert result.status_code == 422, (changes, result.text)
