"""授权结果确认前不能自动写入调用方已经暂存的实体。"""


import pytest
from sqlalchemy import delete, event, func, select

from app.core.errors import ForbiddenError, NotFoundError, UnauthorizedError
from app.models import AuthorizationState, Chapter, Project
from app.services import authorization_service, project_access
from tests.helpers import create_project, register


@pytest.mark.parametrize("invalid_state", ["missing_user", "missing_authorization_state"])
async def test_authentication_failure_never_flushes_pending_entity(app, client, invalid_state):
    owner = await register(client, "auth-autoflush-owner")
    project = await create_project(client)
    if invalid_state == "missing_authorization_state":
        async with app.state.sessionmaker() as db:
            await db.execute(delete(AuthorizationState))
            await db.commit()
    actor_id = 2_147_483_647 if invalid_state == "missing_user" else owner["id"]
    error = UnauthorizedError if invalid_state == "missing_user" else authorization_service.AuthorizationStorageError
    async with app.state.sessionmaker() as db:
        pending = Chapter(project_id=project["id"], title="认证失败不得写入")
        db.add(pending)
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        with pytest.raises(error):
            await authorization_service.require_authenticated(db, actor_id)
        assert flushes == [] and pending.id is None and pending in db.new
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Chapter).where(
            Chapter.project_id == project["id"], Chapter.title == "认证失败不得写入",
        )) == 0


@pytest.mark.parametrize("for_update", [False, True])
@pytest.mark.parametrize("actor_kind", ["missing", "nonmember", "viewer"])
async def test_denied_access_never_flushes_pending_entity(app, client, make_client, for_update, actor_kind):
    await register(client, "autoflush-owner")
    guest = make_client()
    guest_user = await register(guest, "autoflush-viewer")
    stranger_user = await register(make_client(), "autoflush-stranger")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    invite = await client.post(f"{base}/collaborators", json={"username": "autoflush-viewer", "role": "viewer"})
    assert invite.status_code == 201, invite.text
    accept = f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept"
    assert (await guest.post(accept, json={"expected_revision": 1})).status_code == 200
    actor_id, error = {
        "missing": (2_147_483_647, UnauthorizedError),
        "nonmember": (stranger_user["id"], NotFoundError),
        "viewer": (guest_user["id"], ForbiddenError),
    }[actor_kind]
    async with app.state.sessionmaker() as db:
        pending = Chapter(project_id=project["id"], title="不得提前写入")
        db.add(pending)
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        with pytest.raises(error):
            await project_access.require_project(db, actor_id, project["id"], "content.edit", for_update=for_update)
        assert flushes == []
        assert pending.id is None and pending in db.new
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Chapter).where(
            Chapter.project_id == project["id"], Chapter.title == "不得提前写入",
        )) == 0


async def test_authorized_resolution_preserves_pending_project_and_entity(app, client):
    owner = await register(client, "autoflush-authorized")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        record = await db.get(Project, project["id"])
        record.title = "尚未提交的标题"
        pending = Chapter(project_id=project["id"], title="授权后的章节")
        db.add(pending)
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        resolved = await project_access.require_project(db, owner["id"], project["id"], "content.edit", for_update=True)
        assert resolved is record and record.title == "尚未提交的标题"
        assert pending.id is None and pending in db.new and flushes == []
        assert db.autoflush
        chapter_id = await db.scalar(select(Chapter.id).where(Chapter.title == "授权后的章节"))
        assert chapter_id == pending.id and chapter_id is not None and flushes == [True]
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project["id"])).title == "尚未提交的标题"
        assert await db.scalar(select(Chapter.id).where(Chapter.title == "授权后的章节")) is not None
