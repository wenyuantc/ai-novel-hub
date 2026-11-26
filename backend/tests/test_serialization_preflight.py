"""用实际词表及连载写入口核对稿件、操作者、确认时效和分平台登记。"""

from uuid import uuid4

from tests.helpers import register
from tests.test_rich_document_api import add_section, save_payload
from tests.test_serialization_write import confirmation, preview, registration, setup_workbench


async def set_terms(client, terms, revision=0, *, site=False):
    path = "/api/v1/admin/publication-terms" if site else "/api/v1/publication-terms"
    result = await client.put(path, json={"expected_revision": revision, "terms": terms})
    assert result.status_code == 200, result.text


def review(result, *, stale=False):
    assert result.status_code == 409, result.text
    envelope = result.json()
    assert envelope["code"] == ("stale_serialization_preflight" if stale else "serialization_preflight_required")
    return envelope["preflight"]


async def add_collaborator(client, guest, base, role):
    user = await register(guest, f"publication-{role}")
    members = base.removesuffix("/serialization") + "/collaborators"
    invite = (await client.post(members, json={"username": user["username"], "role": role})).json()
    accepted = await guest.post(f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/accept",
                                json={"expected_revision": invite["revision"]})
    assert accepted.is_success, accepted.text


async def test_hits_use_owner_terms_and_frozen_reader_projection(client, system_admin, make_client):
    base, chapter, url, scene = await setup_workbench(client, "🙂甲乙甲乙\n丙")
    doc_path = f"{url}/scenes/{scene['id']}/document"
    doc = save_payload((await client.get(doc_path)).json())
    add_section(doc)
    doc["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 1, "end": 2}]
    assert (await client.put(doc_path, json=doc)).is_success
    await set_terms(system_admin, ["第", "乙", "甲乙"], site=True)
    await set_terms(client, ["甲乙", "作者🙂", "私人未命中"])
    editor = make_client()
    await add_collaborator(client, editor, base, "editor")
    await set_terms(editor, ["丙"])
    outsider = make_client()
    await register(outsider, "preflight-outsider")
    finalize = f"{chapter}/editions"
    body = confirmation(await preview(client, chapter))
    assert (await outsider.post(finalize, json=body)).status_code == 404
    viewer = make_client()
    await add_collaborator(client, viewer, base, "viewer")
    assert (await viewer.get(chapter)).is_success
    forbidden = await viewer.post(finalize, json=body, params={"preflight_offset": 0})
    assert forbidden.status_code == 403 and "preflight" not in forbidden.json()
    owner = review(await client.post(finalize, json=body))
    editor_page = review(await editor.post(finalize, json=body))
    assert editor_page["hits"] == owner["hits"]
    assert editor_page["total"] == 5
    assert [(h["field"], h["term"], h["start"], h["end"]) for h in owner["hits"]] == [
        ("title", "第", 0, 1), ("content", "甲乙", 1, 3), ("content", "乙", 2, 3),
        ("content", "甲乙", 3, 5), ("content", "乙", 4, 5),
    ]
    assert "私人未命中" not in str(editor_page) and "作者🙂" not in str(editor_page)
    review(await editor.post(finalize, json=body | {"confirmation_token": owner["confirmation_token"]}), stale=True)
    assert (await preview(client, chapter))["editions"] == []
    saved = await editor.post(finalize, json=body | {"confirmation_token": editor_page["confirmation_token"]})
    assert saved.is_success, saved.text


async def test_confirmations_expire_on_changes_and_removal_of_last_match(client):
    _base, chapter, url, _scene = await setup_workbench(client, "甲" * 52)
    await set_terms(client, ["甲"])
    path = f"{chapter}/editions"
    data = confirmation(await preview(client, chapter))
    first = review(await client.post(path, json=data))
    assert len(first["hits"]) == first["limit"] == 50 and first["total"] == 52
    page2 = review(await client.post(path, json=data, params={"preflight_offset": 50}))
    assert first["confirmation_token"] == page2["confirmation_token"]
    assert [hit["start"] for hit in page2["hits"]] == [50, 51]
    await set_terms(client, [], 1)
    after = review(await client.post(path, json=data | {"confirmation_token": first["confirmation_token"]}), stale=True)
    assert after["total"] == 0 and after["hits"] == []
    empty_page = review(await client.post(path, json=data, params={"preflight_offset": 50}))
    assert empty_page["confirmation_token"] == after["confirmation_token"]
    assert (await preview(client, chapter))["editions"] == []
    assert (await client.patch(url, json={"content": "乙", "expected_content_revision": 0})).is_success
    stale = await client.post(path, json=data | {"confirmation_token": after["confirmation_token"]})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_serialization_draft"
    assert (await preview(client, chapter))["editions"] == []


async def test_unpublished_frozen_txt_and_retry_after_word_list_or_platform_changes(client):
    base, chapter, url, _scene = await setup_workbench(client, "冻稿🙂\n第二段")
    edition = (await client.post(f"{chapter}/editions", json=confirmation(await preview(client, chapter)))).json()
    assert (await client.patch(url, json={"content": "新文", "expected_content_revision": 0})).is_success
    download = await client.get(f"{chapter}/editions/{edition['id']}/export")
    assert download.is_success
    assert "attachment" in download.headers["content-disposition"]
    assert download.content.decode("utf-8") == "第一章\n\n冻稿🙂\n第二段\n"
    await set_terms(client, ["冻稿", "新文"])
    path = f"{chapter}/publications"
    data = registration(edition["id"], 1)
    first = review(await client.post(path, json=data))
    assert [hit["term"] for hit in first["hits"]] == ["冻稿"]
    signed = data | {"confirmation_token": first["confirmation_token"]}
    for changes in ({"channel": "其他"}, {"request_id": str(uuid4())}):
        review(await client.post(path, json=signed | changes), stale=True)
    assert (await client.get(f"{base}/overview?month=2026-01")).json()["stock_count"] == 1
    assert (await client.get(f"{base}/publications")).json()["total"] == 0
    posted = await client.post(path, json=signed)
    assert posted.is_success, posted.text
    await set_terms(client, ["第二"], 1)
    assert (await client.post(path, json=signed)).json() == posted.json()
    assert (await client.get(f"{base}/publications")).json()["total"] == 1
    assert (await client.get(f"{base}/overview?month=2026-01")).json()["stock_count"] == 0


async def test_management_preflight_and_paging_do_not_record_failed_edits(app, client, system_admin):
    from sqlalchemy import select

    from app.models import AdminAuditLog

    _base, chapter, _url, _scene = await setup_workbench(client, "甲" * 52)
    await set_terms(client, ["甲"])
    path = f"{chapter}/editions"
    detail = await preview(system_admin, chapter)
    payload = confirmation(detail)
    first = review(await system_admin.post(path, json=payload))
    review(await system_admin.post(path, json=payload, params={"preflight_offset": 50}))
    review(await system_admin.post(path, json=payload | {"confirmation_token": "0" * 64}), stale=True)
    result = await system_admin.post(path, json=payload | {"confirmation_token": first["confirmation_token"]})
    assert result.is_success
    async with app.state.sessionmaker() as db:
        logs = list(await db.scalars(select(AdminAuditLog.result).where(
            AdminAuditLog.action == "chapter.update", AdminAuditLog.chapter_id == detail["chapter"]["chapter_id"],
        ).order_by(AdminAuditLog.id)))
        assert logs == ["failure", "success"]


def test_dense_overlaps_are_counted_exactly_and_paginated_in_manuscript_order():
    from app.services.publication_term_matching import PAGE_SIZE, hit_page

    terms = ["甲" * n for n in range(1, 129)]
    text = "甲" * 10_000
    total = sum(len(text) - len(term) + 1 for term in terms)
    offset = total - 30
    count, hits = hit_page("", text, terms, offset)
    assert count == total and len(hits) == 30 < PAGE_SIZE
    assert [(hit["start"], hit["end"]) for hit in hits] == [
        (i, j) for i in range(len(text) - 8, len(text)) for j in range(i + 1, len(text) + 1)
    ][-30:]
