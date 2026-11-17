"""六类世界观均通过实际 HTTP 图片接口管理缩略图。"""

import pytest

from tests.helpers import create_project, register
from tests.test_setting_thumbnails import image_data


@pytest.mark.parametrize("category", ["place", "faction", "item", "rule", "history", "other"])
async def test_each_world_category_supports_thumbnail_lifecycle(client, category):
    await register(client, f"thumbnail-world-{category}")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    created = await client.post(f"{base}/world", json={"title": f"类别 {category}", "category": category})
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]
    image_url = f"{base}/settings/world/{item_id}/thumbnail"
    image = await client.put(image_url, files={"file": ("sample.png", image_data(), "image/png")},
                             data={"expected_version": ""})
    assert image.status_code == 200, image.text
    meta = image.json()
    listing = await client.get(f"{base}/world", params={"category": category})
    assert listing.status_code == 200, listing.text
    assert [(row["id"], row["thumbnail"]) for row in listing.json()] == [(item_id, meta)]
    detail = await client.get(f"{base}/world/{item_id}")
    assert detail.status_code == 200 and detail.json()["thumbnail"] == meta
    read = await client.get(image_url)
    assert read.status_code == 200 and read.headers["etag"] == f'"{meta["version"]}"'
    replaced = await client.put(image_url, files={"file": ("other.png", image_data("blue"), "image/png")},
                                data={"expected_version": meta["version"]})
    assert replaced.status_code == 200 and replaced.json()["version"] != meta["version"]
    removed = await client.delete(image_url, params={"expected_version": replaced.json()["version"]})
    assert removed.status_code == 200
    assert (await client.get(image_url)).status_code == 404
