"""独立检查：两个请求首次保存同一自定义类型、同一角色对时的竞争。"""

import asyncio

from tests.helpers import create_project, register

pytest_plugins = ("tests.conftest",)


async def test_simultaneous_custom_relation_type(app, client, make_client, monkeypatch):
    from app.services import relationship_service as svc

    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    characters = []
    for name in ("甲", "乙"):
        response = await client.post(f"{base}/characters", json={"name": name})
        assert response.status_code == 201
        characters.append(response.json()["id"])
    second = make_client()
    assert (
        await second.post("/api/v1/auth/login", json={"username": "alice", "password": "secret123"})
    ).status_code == 200
    original = svc.find_type
    arrived = 0
    ready = asyncio.Event()

    async def simultaneous_find_type(db, project_id, role_a, role_b):
        nonlocal arrived
        found = await original(db, project_id, role_a, role_b)
        if found[0] is None:
            arrived += 1
            if arrived == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 5)
        return found

    monkeypatch.setattr(svc, "find_type", simultaneous_find_type)
    body = {
        "items": [{
            "a_id": characters[0], "b_id": characters[1],
            "role_a": "引路人", "role_b": "同行者", "source": "ai_generate",
        }],
    }
    responses = await asyncio.gather(
        client.post(f"{base}/relationships/batch", json=body),
        second.post(f"{base}/relationships/batch", json=body),
        return_exceptions=True,
    )
    assert all(not isinstance(r, BaseException) for r in responses), responses
    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]
