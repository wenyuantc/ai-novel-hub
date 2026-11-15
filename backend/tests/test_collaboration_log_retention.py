"""协作者的日志裁剪不能删除他人或未知归属的生成记录。"""

from sqlalchemy import select

from app.models import GenerationLog
from app.services import generation_log_service
from tests.helpers import create_project, register


async def test_log_retention_is_scoped_to_project_and_actor(app, client, make_client, monkeypatch):
    owner = await register(client, "log-retention-owner")
    collaborator = await register(make_client(), "log-retention-editor")
    project = await create_project(client)
    other_project = await create_project(client, title="另一部作品")
    monkeypatch.setattr(generation_log_service, "MAX_LOGS_PER_PROJECT", 2)

    async with app.state.sessionmaker() as db:
        unknown = GenerationLog(
            project_id=project["id"], actor_user_id=None, kind="generate", status="success",
        )
        db.add(unknown)
        await db.flush()
        unknown_id = unknown.id

        async def add(actor_id: int, label: str, project_id: int = project["id"]) -> int:
            row = await generation_log_service.write_log(
                db, project_id=project_id, actor_user_id=actor_id, chapter_id=None,
                kind="generate", ai_config_id=None, provider="test", model="test",
                params={"label": label}, messages=[], status="success",
            )
            return row.id

        other_id = await add(owner["id"], "other-project", other_project["id"])
        first_owner = await add(owner["id"], "owner-1")
        editor_ids = [await add(collaborator["id"], f"editor-{i}") for i in range(3)]
        owner_ids = [await add(owner["id"], f"owner-{i}") for i in range(2, 4)]
        await db.commit()

        rows = list(await db.scalars(select(GenerationLog).order_by(GenerationLog.id)))
        assert {row.id for row in rows if row.actor_user_id is None} == {unknown_id}
        assert {row.id for row in rows if row.actor_user_id == owner["id"]} == set(owner_ids) | {other_id}
        assert {row.id for row in rows if row.actor_user_id == collaborator["id"]} == set(editor_ids[1:])
        assert first_owner not in {row.id for row in rows}
