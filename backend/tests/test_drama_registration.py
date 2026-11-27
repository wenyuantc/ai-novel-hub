from app.plugins import load
from tests.helpers import create_project, register


def test_drama_registers_four_tasks():
    registry = load()
    manifest = registry.get("drama")
    assert manifest is not None
    kinds = {kind for handler in manifest.task_handlers for kind in handler.kinds}
    assert kinds == {"drama.rewrite", "drama.extract", "drama.storyboard", "drama.prompts"}
    assert all(registry.plugin_of_kind(kind) == "drama" for kind in kinds)


async def test_drama_switch_preserves_series(client, system_admin):
    await register(client)
    project = await create_project(client)
    admin_url = "/api/v1/admin/plugins"
    series_url = f"/api/v1/plugins/drama/projects/{project['id']}/series"

    listing = await system_admin.get(admin_url)
    assert listing.is_success
    plugin = next(item for item in listing.json()["items"] if item["id"] == "drama")
    assert plugin["enabled"] is False
    assert plugin["revision"] == 0

    async def switch(revision: int, enabled: bool):
        response = await system_admin.patch(
            f"{admin_url}/drama", json={"revision": revision, "enabled": enabled}
        )
        assert response.is_success, response.text
        config = (await client.get("/api/v1/auth/config")).json()
        assert ("drama" in config["enabled_plugins"]) is enabled

    async def assert_disabled():
        response = await client.get(series_url)
        assert response.status_code == 403
        assert response.json()["code"] == "plugin_disabled"

    await assert_disabled()
    await switch(0, True)
    assert (await client.get(series_url)).json() == []
    created = await client.post(series_url, json={"title": "灯下书"})
    assert created.status_code == 201, created.text
    await switch(1, False)
    await assert_disabled()
    await switch(2, True)
    listing = (await client.get(series_url)).json()
    assert [(item["id"], item["title"]) for item in listing] == [(created.json()["id"], "灯下书")]
