"""区块转换写权限和章节风格投影的协作边界。"""

from uuid import UUID

import pytest
from sqlalchemy import event, select
from sqlalchemy import inspect as sa_inspect

from app.core.errors import ForbiddenError
from app.models import Chapter
from app.services import chapter_service, rich_document_conversion, rich_document_service
from tests.helpers import register
from tests.rbac_helpers import assign_roles, create_role
from tests.test_collaboration_content_access import _content_setup
from tests.test_rich_document_api import KEY, add_section, save_payload


async def _document_with_section(client, viewer, chapter_url, scene):
    url = f"{chapter_url}/scenes/{scene['id']}/document"
    document = await viewer.get(url)
    assert document.status_code == 200, document.text
    body = save_payload(document.json())
    add_section(body, enabled=True)
    saved = await client.put(url, json=body)
    assert saved.status_code == 200, saved.text
    return url, saved.json()


async def test_conversion_checks_edit_before_revision_and_respects_admin_union(client, make_client, system_admin):
    (_, viewer_user, _, _, _, scene, _, chapter_url, viewer, editor, outsider) = await _content_setup(
        client, make_client,
    )
    document_url, saved = await _document_with_section(client, viewer, chapter_url, scene)
    url = f"{document_url}/sections/{KEY}/convert"
    revision = saved["content_revision"]
    for actor, status in ((viewer, 403), (outsider, 404)):
        for expected in (revision, revision + 1):
            denied = await actor.post(url, json={"expected_content_revision": expected})
            assert denied.status_code == status, denied.text
    assert (await client.get(document_url)).json() == saved
    for actor in (client, editor):
        stale = await actor.post(url, json={"expected_content_revision": revision + 1})
        assert stale.status_code == 409 and stale.json()["code"] == "stale_scene_revision", stale.text

    role = await create_role(system_admin, "转换文档管理员", ["projects.read", "projects.edit"])
    await assign_roles(system_admin, viewer_user["id"], [role["id"]])
    stale = await viewer.post(url, json={"expected_content_revision": revision + 1})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_scene_revision", stale.text
    converted = await viewer.post(url, json={"expected_content_revision": revision})
    assert converted.status_code == 200, converted.text
    assert "作者🙂" in converted.json()["document"]["content"]


async def test_conversion_rechecks_edit_when_editor_is_revoked_before_save(client, make_client, monkeypatch):
    (_, _, editor_user, project, _, scene, base, chapter_url, viewer, editor, _) = await _content_setup(
        client, make_client,
    )
    document_url, saved = await _document_with_section(client, viewer, chapter_url, scene)
    original = rich_document_service.save_document
    intercepted = False

    async def revoke_before_save(*args, **kwargs):
        nonlocal intercepted
        intercepted = True
        revoked = await client.delete(f"{base}/collaborators/{editor_user['id']}", params={"expected_revision": 2})
        assert revoked.status_code == 204, revoked.text
        return await original(*args, **kwargs)

    monkeypatch.setattr(rich_document_service, "save_document", revoke_before_save)
    result = await editor.post(f"{document_url}/sections/{KEY}/convert", json={
        "expected_content_revision": saved["content_revision"],
    })
    assert intercepted and result.status_code == 404, result.text
    assert (await client.get(document_url)).json() == saved
    assert (await editor.get(f"/api/v1/projects/{project['id']}")).status_code == 404


async def test_denied_conversion_does_not_flush_callers_pending_state(app, client, make_client):
    (_, viewer_user, _, project, chapter, scene, _, _, _, _, _) = await _content_setup(client, make_client)
    async with app.state.sessionmaker() as db:
        row = await db.get(Chapter, chapter["id"])
        row.title = "不应被提前提交的草稿"

        def forbid_flush(*_args):
            raise AssertionError("转换授权前不能自动 flush")

        event.listen(db.sync_session, "before_flush", forbid_flush)
        try:
            with pytest.raises(ForbiddenError):
                await rich_document_conversion.convert_section(
                    db, viewer_user["id"], project["id"], chapter["id"], scene["id"], UUID(KEY), 999,
                )
            assert sa_inspect(row).attrs.title.history.has_changes()
        finally:
            event.remove(db.sync_session, "before_flush", forbid_flush)
            await db.rollback()


async def test_chapter_style_projection_filters_owner_admin_collaborator_and_preserves_binding(
    app, client, make_client, system_admin,
):
    (owner_user, viewer_user, editor_user, project, first, _, base, _, viewer, editor, outsider) = await _content_setup(
        client, make_client,
    )
    foreign = make_client()
    await register(foreign, "third-party-style-owner")

    async def create_style(actor, name):
        response = await actor.post("/api/v1/styles", json={"name": name})
        assert response.status_code == 201, response.text
        return response.json()["id"]

    styles = {
        "owner": project["default_style_id"],
        "viewer": await create_style(viewer, "查看者私人风格"),
        "editor": await create_style(editor, "编辑者私人风格"),
        "admin": await create_style(system_admin, "管理员私人风格"),
        "other": await create_style(foreign, "第三方私人风格"),
    }
    styles["global"] = next(style["id"] for style in (await client.get("/api/v1/styles")).json() if style["is_preset"])
    chapters = {"owner": first["id"]}
    for kind in ("viewer", "editor", "admin", "other", "global"):
        result = await client.post(f"{base}/chapters", json={"title": kind, "content": f"{kind} 正文"})
        assert result.status_code == 201, result.text
        chapters[kind] = result.json()["id"]
    async with app.state.sessionmaker() as db:
        for kind, chapter_id in chapters.items():
            (await db.get(Chapter, chapter_id)).style_id = styles[kind]
        await db.commit()

    async def assert_views(actor, visible):
        listing = await actor.get(f"{base}/chapters")
        assert listing.status_code == 200, listing.text
        listed = {item["id"]: item["style_id"] for item in listing.json()}
        for kind, chapter_id in chapters.items():
            expected = styles[kind] if kind in visible else None
            assert listed[chapter_id] == expected, (kind, listed[chapter_id], expected)
            detail = await actor.get(f"{base}/chapters/{chapter_id}")
            assert detail.status_code == 200 and detail.json()["style_id"] == expected, detail.text

    await assert_views(client, {"owner", "global"})
    await assert_views(viewer, {"viewer", "global"})
    await assert_views(editor, {"editor", "global"})
    await assert_views(system_admin, {"owner", "admin", "global"})
    assert (await outsider.get(f"{base}/chapters")).status_code == 404
    assert (await foreign.get(f"{base}/chapters/{chapters['other']}")).status_code == 404

    role = await create_role(system_admin, "风格查看管理员", ["projects.read", "projects.edit"])
    await assign_roles(system_admin, viewer_user["id"], [role["id"]])
    await assert_views(viewer, {"owner", "viewer", "global"})
    async with app.state.sessionmaker() as db:
        rows = await db.execute(select(Chapter.id, Chapter.style_id).where(Chapter.id.in_(chapters.values())))
        assert dict(rows.all()) == {chapter_id: styles[kind] for kind, chapter_id in chapters.items()}

    admin_id = (await system_admin.get("/api/v1/auth/me")).json()["id"]
    for user_id in (owner_user["id"], viewer_user["id"], editor_user["id"], admin_id):
        async with app.state.sessionmaker() as db:
            row = await db.get(Chapter, chapters["other"])
            row.title = "未提交草稿"

            def forbid_flush(*_args):
                raise AssertionError("风格投影不能自动 flush 调用方的暂存变更")

            event.listen(db.sync_session, "before_flush", forbid_flush)
            try:
                views = await chapter_service.manual_chapter_views(db, user_id, project["id"], [row], full=True)
                assert views[0].style_id is None and views[0].title == "未提交草稿"
                assert sa_inspect(row).attrs.title.history.has_changes()
            finally:
                event.remove(db.sync_session, "before_flush", forbid_flush)
                await db.rollback()
