from io import BytesIO

from docx import Document

from tests.helpers import create_project, register

BASE = "/api/v1/plugins/drama"


async def setup_series(client, admin):
    await register(client, "drama-author")
    assert (await admin.patch("/api/v1/admin/plugins/drama", json={"enabled": True, "revision": 0})).is_success
    project_id = (await create_project(client))["id"]
    url = f"{BASE}/projects/{project_id}/series"
    response = await client.post(url, json={"title": "探案"})
    assert response.status_code == 201, response.text
    return project_id, response.json()["id"]


async def test_chapter_snapshot_permissions_and_export(client, make_client, system_admin):
    pid, sid = await setup_series(client, system_admin)
    url = f"{BASE}/series/{sid}/episodes"
    chapters = []
    for number, content in enumerate(["第一章的内容", "月下的案发现场", "新线索"], 1):
        response = await client.post(
            f"/api/v1/projects/{pid}/chapters",
            json={
                "chapter_no": number,
                "title": f"第{number}章",
                "content": content,
            },
        )
        assert response.is_success, response.text
        chapters.append(response.json()["id"])
    invalid = await client.post(url, json={"title": "跳章", "chapter_ids": chapters[::2]})
    assert invalid.status_code == 400
    created = await client.post(url, json={"title": "月下", "chapter_ids": chapters[:2]})
    assert created.status_code == 201, created.text
    ep = created.json()
    assert ep["source_text"] == "第一章的内容\n\n月下的案发现场"
    assert ep["chapter_ids"] == chapters[:2]
    eurl = f"{BASE}/episodes/{ep['id']}"
    original_url = f"/api/v1/projects/{pid}/chapters/{chapters[0]}"
    scene_page = (await client.get(original_url + "/scenes")).json()
    edited = await client.patch(
        original_url + "/scenes/" + str(scene_page["items"][0]["id"]),
        json={
            "expected_content_revision": scene_page["content_revision"],
            "content": "后来修改的章节",
        },
    )
    assert edited.is_success, edited.text
    assert (await client.get(eurl)).json()["source_text"] == ep["source_text"]
    assert (await client.delete(original_url)).is_success
    assert (await client.get(eurl)).json()["source_text"] == ep["source_text"]
    patch = {"revision": ep["revision"], "script": "月光洒在脚印上。"}
    assert (await client.patch(eurl, json=patch)).is_success
    assert (await client.patch(eurl, json=patch)).status_code == 409
    for fmt in ("md", "docx"):
        exported = await client.get(eurl + "/export", params={"format": fmt, "parts": "script"})
        assert exported.is_success, exported.text[:150] if fmt == "md" else exported.status_code
        content = (
            exported.text if fmt == "md" else "\n".join(p.text for p in Document(BytesIO(exported.content)).paragraphs)
        )
        assert "月光洒在脚印上。" in content
    guest = make_client()
    await register(guest, "drama-reader")
    assert (await guest.get(eurl)).status_code == 404
    invitation = (
        await client.post(
            f"/api/v1/projects/{pid}/collaborators",
            json={
                "username": "drama-reader",
                "role": "viewer",
            },
        )
    ).json()
    assert (
        await guest.post(
            f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
            json={"expected_revision": invitation["revision"]},
        )
    ).is_success
    assert (await guest.get(eurl)).is_success
    assert (await guest.patch(eurl, json=patch)).status_code in (403, 404)
    assert (await system_admin.get(eurl + "/export")).status_code == 404


async def test_shots_order_requires_current_revision_and_exact_membership(client, system_admin):
    _, sid = await setup_series(client, system_admin)
    ep = (await client.post(f"{BASE}/series/{sid}/episodes", json={"title": "一集", "source_text": "原文"})).json()
    eurl = f"{BASE}/episodes/{ep['id']}"
    ids = []
    for description in ("敲门", "推门"):
        shot = await client.post(eurl + "/shots", json={"description": description})
        assert shot.status_code == 201, shot.text
        ids.append(shot.json()["id"])
    order = eurl + "/shots/order"
    current = (await client.get(eurl)).json()["revision"]
    assert (await client.put(order, json={"revision": ep["revision"], "shot_ids": ids})).status_code == 409
    assert (await client.put(order, json={"revision": current, "shot_ids": ids[:1]})).status_code == 400
    reversed_order = await client.put(order, json={"revision": current, "shot_ids": ids[::-1]})
    assert reversed_order.is_success, reversed_order.text
    assert [row["id"] for row in (await client.get(eurl + "/shots")).json()] == ids[::-1]
    exported = await client.get(eurl + "/export", params={"format": "docx", "parts": "shots"})
    assert exported.is_success, exported.status_code
    table, = Document(BytesIO(exported.content)).tables
    assert [row.cells[1].text for row in table.rows] == ["画面与台词", "推门", "敲门"]
    markdown = await client.get(eurl + "/export", params={"format": "md", "parts": "shots"})
    assert "| 镜头 | 画面与台词 |" in markdown.text and markdown.text.index("推门") < markdown.text.index("敲门")
    shot = reversed_order.json()[0]
    invalid = await client.patch(f"{BASE}/shots/{shot['id']}", json={"revision": shot["revision"], "prop_ids": [999]})
    assert invalid.status_code == 404


async def test_foreign_chapter_rejected_and_damaged_link_blocks_delete(app, client, system_admin):
    from app.plugins.drama.models import DramaEpisodeSource

    _, sid = await setup_series(client, system_admin)
    other_pid = (await create_project(client, title="另一部作品"))["id"]
    base = f"/api/v1/projects/{other_pid}/chapters"
    chapter = (await client.post(base, json={"title": "另一章", "content": "不同作品"})).json()
    url = f"{BASE}/series/{sid}/episodes"
    denied = await client.post(url, json={"title": "第一集", "chapter_ids": [chapter["id"]]})
    assert denied.status_code == 404
    episode = (await client.post(url, json={"title": "独立剧本"})).json()
    # 模拟历史脏数据；用户删章不能清除另一本书的来源记录。
    async with app.state.sessionmaker() as db:
        db.add(DramaEpisodeSource(episode_id=episode["id"], chapter_id=chapter["id"], chapter_title="旧标题", sort=1))
        await db.commit()
    removed = await client.delete(base + f"/{chapter['id']}")
    assert removed.status_code == 409 and removed.json()["code"] == "chapter_reference_invalid"
    assert (await client.get(base + f"/{chapter['id']}")).is_success
