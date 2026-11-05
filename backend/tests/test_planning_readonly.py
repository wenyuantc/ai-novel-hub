"""规划读模型不能顺手保存调用方尚未提交的业务对象。"""

import pytest
from sqlalchemy import event, select

from app.models import Character, Project
from app.services import planning_service
from tests.helpers import create_project, register


@pytest.mark.parametrize("operation", ["matrix", "weave", "entries", "detached", "tags", "subplots", "crossings"])
async def test_planning_read_preserves_pending_objects(client, app, operation):
    await register(client)
    project = await create_project(client)
    pid = project["id"]
    base = f"/api/v1/projects/{pid}"
    assert (await client.post(f"{base}/chapters", json={"title": "首章"})).status_code == 201
    assert (await client.post(f"{base}/characters", json={"name": "林岚"})).status_code == 201
    async with app.state.sessionmaker() as db:
        owner_id = await db.scalar(select(Project.user_id).where(Project.id == pid))
        pending = Character(project_id=pid, name="尚未保存的人物")
        db.add(pending)

        def prevent_flush(*_):
            raise AssertionError("规划查询触发了调用方待写对象的 flush")

        event.listen(db.sync_session, "before_flush", prevent_flush)
        try:
            reader = getattr(planning_service, operation if operation in {"matrix", "weave"} else f"list_{operation}")
            await reader(db, owner_id, pid)
            assert pending in db.new and pending.id is None
        finally:
            event.remove(db.sync_session, "before_flush", prevent_flush)
