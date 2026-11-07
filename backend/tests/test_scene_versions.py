"""新结构历史可撤回，旧纯文本只能明确提取；不回滚独立设定事实。"""

import pytest
from sqlalchemy import delete, select

from app.models import ChapterVersion, Scene
from tests.test_scenes import setup_scene


async def test_restore_structural_change_without_text_change_and_restore_itself(app, client):
    _, _, url, first = await setup_scene(client, "甲🙂乙\n 尾")
    response = await client.post(f"{url}/scenes/{first['id']}/split", json={
        "expected_content_revision": 0, "offset": 2,
    })
    assert response.status_code == 200, response.text
    right = response.json()["items"][1]
    versions = (await client.get(f"{url}/versions")).json()
    assert len(versions) == 2 and all(row["has_scene_snapshot"] for row in versions)
    assert all("scene_snapshot" not in row and "content" not in row for row in versions)
    original = versions[1]
    preview = await client.get(f"{url}/versions/{original['id']}/restore-preview?expected_content_revision=1")
    assert preview.status_code == 200 and preview.json()["retiring_scene_ids"] == [right["id"]]
    restored = await client.post(f"{url}/versions/{original['id']}/restore", json={"expected_content_revision": 1})
    assert restored.status_code == 200, restored.text
    assert restored.json()["content_revision"] == 2
    page = (await client.get(f"{url}/scenes")).json()
    assert page["total"] == 1 and page["items"][0]["id"] == first["id"]
    again = await client.post(f"{url}/versions/{versions[0]['id']}/restore", json={"expected_content_revision": 2})
    assert again.status_code == 200, again.text
    assert [row["id"] for row in (await client.get(f"{url}/scenes")).json()["items"]] == [first["id"], right["id"]]
    async with app.state.sessionmaker() as db:
        assert len(list(await db.scalars(select(Scene)))) == 2


async def test_empty_structure_and_clear_are_real_version_boundaries(client):
    _, _, url, first = await setup_scene(client, "")
    created = await client.post(f"{url}/scenes", json={"expected_content_revision": 0})
    assert created.status_code == 201, created.text
    versions = (await client.get(f"{url}/versions")).json()
    assert len(versions) == 2 and all(row["word_count"] == 0 for row in versions)
    original = versions[1]
    restored = await client.post(f"{url}/versions/{original['id']}/restore", json={"expected_content_revision": 1})
    assert restored.status_code == 200, restored.text
    changed = await client.patch(f"{url}/scenes/{first['id']}", json={
        "expected_content_revision": 2, "content": "正文",
    })
    assert changed.status_code == 200, changed.text
    cleared = await client.patch(f"{url}/scenes/{first['id']}", json={
        "expected_content_revision": 3, "content": "",
    })
    assert cleared.status_code == 200
    versions = (await client.get(f"{url}/versions")).json()
    assert versions[0]["source"] == "manual" and versions[0]["word_count"] == 0
    assert (await client.get(f"{url}/versions/{versions[1]['id']}")).json()["content"] == "正文"


async def test_legacy_text_restore_single_then_explicit_multiscene_extraction(app, client):
    _, chapter, url, first = await setup_scene(client, "当前正文")
    async with app.state.sessionmaker() as db:
        legacy = ChapterVersion(chapter_id=chapter["id"], seq=2, source="manual", content="旧\r\n稿🙂", word_count=4)
        db.add(legacy)
        await db.commit()
        legacy_id = legacy.id
    restored = await client.post(f"{url}/versions/{legacy_id}/restore")
    assert restored.status_code == 200 and restored.json()["content"] == "旧\r\n稿🙂"
    split = await client.post(f"{url}/scenes/{first['id']}/split", json={
        "expected_content_revision": 1, "offset": 2,
    })
    assert split.status_code == 200, split.text
    right = split.json()["items"][1]
    rejected = await client.post(f"{url}/versions/{legacy_id}/restore", json={"expected_content_revision": 2})
    assert rejected.status_code == 409 and rejected.json()["code"] == "scene_target_required"
    preview = await client.get(f"{url}/versions/{legacy_id}/extract-preview", params={
        "scene_id": right["id"], "expected_content_revision": 2,
    })
    assert preview.status_code == 200, preview.text
    info = preview.json()
    assert info["replacement"] == "旧\r\n稿🙂" and info["original"] == right["content"]
    extracted = await client.post(f"{url}/versions/{legacy_id}/extract", json={
        "scene_id": right["id"], "expected_content_revision": 2, "signature": info["signature"],
    })
    assert extracted.status_code == 200 and extracted.json()["content"] == "旧\r\n稿🙂"
    left = (await client.get(f"{url}/scenes/{first['id']}")).json()
    assert left["content"] == "旧\r"
    stale = await client.post(f"{url}/versions/{legacy_id}/extract", json={
        "scene_id": right["id"], "expected_content_revision": 2, "signature": info["signature"],
    })
    assert stale.status_code == 409 and stale.json()["code"] == "stale_scene_revision"


@pytest.mark.parametrize("cleared", ["", " \n\t"], ids=["empty", "whitespace"])
@pytest.mark.parametrize("has_history", [False, True], ids=["without_history", "with_history"])
async def test_legacy_single_scene_clear_preserves_exact_structural_history(app, client, cleared, has_history):
    original = "\t完整原稿😀\r\n必须可恢复  "
    _, chapter, url, scene = await setup_scene(client, original)
    if not has_history:
        # 合法无历史状态（如不含versions的备份导入）；只删测试历史，不改正文或场景。
        async with app.state.sessionmaker() as db:
            await db.execute(delete(ChapterVersion).where(ChapterVersion.chapter_id == chapter["id"]))
            await db.commit()
    before = (await client.get(url)).json()
    before_versions = (await client.get(f"{url}/versions")).json()
    assert len(before_versions) == int(has_history)
    assert before["content"] == original and before["word_count"] == 10

    response = await client.patch(url, json={
        "content": cleared, "expected_content_revision": before["content_revision"],
    })
    assert response.status_code == 200, response.text
    after = response.json()
    saved_scene = (await client.get(f"{url}/scenes/{scene['id']}")).json()
    assert after["content"] == saved_scene["content"] == cleared
    assert after["word_count"] == saved_scene["word_count"] == 0
    assert after["content_revision"] == saved_scene["content_revision"] == before["content_revision"] + 1
    page = (await client.get(f"{url}/scenes")).json()
    assert page["total"] == 1 and page["items"][0]["id"] == scene["id"]

    versions = (await client.get(f"{url}/versions")).json()
    assert len(versions) == 2 and all(version["has_scene_snapshot"] for version in versions)
    cleared_version, original_version = [
        (await client.get(f"{url}/versions/{version['id']}")).json() for version in versions
    ]
    assert cleared_version["source"] == "manual" and cleared_version["note"] == "清空正文"
    assert cleared_version["content"] == cleared and cleared_version["word_count"] == 0
    assert original_version["content"] == original and original_version["word_count"] == 10
    assert original_version["source"] == ("manual" if has_history else "baseline")
    if has_history:
        assert original_version["id"] == before_versions[0]["id"]
    snapshots = [version["scene_snapshot"] for version in (cleared_version, original_version)]
    assert all(snapshot["schema_version"] == 1 and len(snapshot["scenes"]) == 1 for snapshot in snapshots)
    cleared_scene, original_scene = [snapshot["scenes"][0] for snapshot in snapshots]
    assert cleared_scene["content"] == cleared and original_scene["content"] == original
    stable_fields = ("id", "title", "summary", "sort", "separator_before", "planning")
    assert {key: cleared_scene[key] for key in stable_fields} == {
        key: original_scene[key] for key in stable_fields
    }
    assert original_scene["id"] == scene["id"]

    restored = await client.post(f"{url}/versions/{original_version['id']}/restore", json={
        "expected_content_revision": after["content_revision"],
    })
    assert restored.status_code == 200, restored.text
    assert restored.json()["content"] == original and restored.json()["word_count"] == 10
    assert restored.json()["content_revision"] == after["content_revision"] + 1
    restored_scene = (await client.get(f"{url}/scenes/{scene['id']}")).json()
    assert restored_scene["id"] == scene["id"] and restored_scene["content"] == original
    assert (await client.get(f"{url}/versions/{cleared_version['id']}")).json()["content"] == cleared
