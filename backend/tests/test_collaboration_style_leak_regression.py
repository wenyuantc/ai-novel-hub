"""章节提示词禁止把作者私人默认风格作为协作者可见内容。"""

import pytest
from sqlalchemy import select

from app.models import Chapter, Project, WritingStyle
from app.services import context_builder
from tests.helpers import create_project, register


async def test_chapter_context_style_visibility_and_owner_fallback_negative_control(
    app, client, make_client, fake_llm, monkeypatch,
):
    owner = await register(client, "style-leak-owner")
    editor_client = make_client()
    editor = await register(editor_client, "style-leak-editor")
    style = await client.post("/api/v1/styles", json={
        "name": "ONLY_OWNER_STYLE_NAME", "description": "ONLY_OWNER_STYLE_DESCRIPTION",
    })
    assert style.status_code == 201, style.text
    project = await create_project(client, style="", default_style_id=style.json()["id"])
    base = f"/api/v1/projects/{project['id']}"
    invitation = await client.post(f"{base}/collaborators", json={
        "username": "style-leak-editor", "role": "editor",
    })
    assert invitation.status_code == 201, invitation.text
    invite = invitation.json()
    accepted = await editor_client.post(
        f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/accept",
        json={"expected_revision": invite["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    chapter = await client.post(f"{base}/chapters", json={"title": "风格边界", "content": "章节正文"})
    assert chapter.status_code == 201, chapter.text

    original = context_builder.project_info

    async def old_owner_fallback(db, project_row, *, actor_user_id):
        # 负控制：模拟旧实现无视真实 actor，把作品作者当作风格读取身份。
        return await original(db, project_row, actor_user_id=project_row.user_id)

    async with app.state.sessionmaker() as db:
        project_row = await db.get(Project, project["id"])
        chapter_row = await db.get(Chapter, chapter.json()["id"])
        monkeypatch.setattr(context_builder, "project_info", old_owner_fallback)
        vulnerable = await context_builder.build_chapter_context(
            db, project_row, chapter_row, 4000, actor_user_id=editor["id"],
        )
        assert "ONLY_OWNER_STYLE_DESCRIPTION" in vulnerable

        monkeypatch.setattr(context_builder, "project_info", original)
        editor_context = await context_builder.build_chapter_context(
            db, project_row, chapter_row, 4000, actor_user_id=editor["id"],
        )
        assert "ONLY_OWNER_STYLE_DESCRIPTION" not in editor_context
        assert "ONLY_OWNER_STYLE_NAME" not in editor_context
        owner_context = await context_builder.build_chapter_context(
            db, project_row, chapter_row, 4000, actor_user_id=owner["id"],
        )
        assert "ONLY_OWNER_STYLE_DESCRIPTION" in owner_context
        with pytest.raises(TypeError):
            await context_builder.build_chapter_context(db, project_row, chapter_row, 4000)
        with pytest.raises(ValueError, match="实际操作者"):
            await context_builder.build_chapter_context(db, project_row, chapter_row, 4000, actor_user_id=None)

        preset = await db.scalar(select(WritingStyle).where(
            WritingStyle.user_id.is_(None), WritingStyle.description != "",
        ).order_by(WritingStyle.id).limit(1))
        assert preset is not None and preset.description.strip()
        project_row.default_style_id = preset.id
        preset_context = await context_builder.build_chapter_context(
            db, project_row, chapter_row, 4000, actor_user_id=editor["id"],
        )
        assert preset.description.strip() in preset_context
    assert not fake_llm.calls and not fake_llm.embed_calls
