"""向导部分替换在真实 PostgreSQL 的事务和项目锁内回滚。"""

import asyncio

import httpx
from sqlalchemy import select, text

from app.models import Character, Project, SettingReferenceHandle, SettingTextField, SettingTimeline
from app.services import wizard_service
from tests.helpers import create_ai_config, create_project, register
from tests.test_collaboration_access_postgresql import _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def test_failed_wizard_replacement_never_commits_partial_changes(postgres_collaboration_apps, monkeypatch):
    owner_app, other_app = postgres_collaboration_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner:
        await register(owner, "wizard-pg-owner")
        project = await create_project(owner)
        base = f"/api/v1/projects/{project['id']}"
        created = await owner.post(f"{base}/characters", json={"name": "原角色"})
        assert created.status_code == 201, created.text

        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}

        class InjectedFailure(Exception):
            pass

        async def fail_after_replacement(db, *_args, **_kwargs):
            await db.flush()
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
            raise InjectedFailure()

        monkeypatch.setattr(wizard_service, "_save_character_relations", fail_after_replacement)

        async def replacement():
            async with owner_app.state.sessionmaker() as db:
                try:
                    await wizard_service.save_wizard_result(db, project["id"], "characters", '[{"name":"新角色"}]')
                except InjectedFailure:
                    # 调用者即使捕获异常后提交，内层 SAVEPOINT 也必须保持回滚。
                    await db.commit()
                else:
                    raise AssertionError("替换过程应在插入新角色后失败")

        async def acquire_project():
            async with other_app.state.sessionmaker() as db:
                await db.execute(select(Project.id).where(Project.id == project["id"]).with_for_update())
                await db.commit()

        work = asyncio.create_task(replacement())
        waiter = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            # 第二连接在替换尚未确认时只能看到原记录，不能看到中间状态。
            async with other_app.state.sessionmaker() as db:
                rows = list(await db.scalars(select(Character).where(Character.project_id == project["id"])))
                assert [row.name for row in rows] == ["原角色"]
            waiter = asyncio.create_task(acquire_project())
            await asyncio.wait_for(_wait_for_blocker(other_app.state.sessionmaker, holder["pid"]), 10)
            release.set()
            await asyncio.wait_for(work, 10)
            await asyncio.wait_for(waiter, 10)
            response = await owner.get(f"{base}/characters")
            assert response.status_code == 200, response.text
            assert [row["name"] for row in response.json()] == ["原角色"]
        finally:
            release.set()
            for pending in (work, waiter):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(*(pending for pending in (work, waiter) if pending is not None),
                                 return_exceptions=True)


async def test_foreign_json_reference_refuses_wizard_before_task(postgres_collaboration_apps, fake_llm):
    owner_app, _ = postgres_collaboration_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner:
        await register(owner, "wizard-pg-reference-owner")
        await create_ai_config(owner)
        project = await create_project(owner)
        foreign = await create_project(owner, title="其他作品")
        base = f"/api/v1/projects/{project['id']}"
        target = await owner.post(f"{base}/characters", json={"name": "本书人物"})
        other = await owner.post(f"/api/v1/projects/{foreign['id']}/characters", json={"name": "外书人物"})
        assert target.status_code == other.status_code == 201
        async with owner_app.state.sessionmaker() as db:
            handle = SettingReferenceHandle(
                project_id=project["id"], kind="character", character_id=target.json()["id"],
            )
            db.add(handle)
            await db.flush()
            timeline = await db.scalar(select(SettingTimeline).where(
                SettingTimeline.project_id == foreign["id"], SettingTimeline.character_id == other.json()["id"],
            ))
            db.add(SettingTextField(timeline_id=timeline.id, label="跨书引用",
                                    base_value={"reference_handle_id": handle.id}))
            await db.commit()
        response = await owner.post(f"{base}/wizard/characters", json={})
        assert response.status_code == 409, response.text
        assert not fake_llm.calls and not fake_llm.embed_calls
        assert [row["name"] for row in (await owner.get(f"{base}/characters")).json()] == ["本书人物"]
