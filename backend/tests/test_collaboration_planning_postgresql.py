"""人工笔记写入与协作者撤权在真实项目行上串行。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text

from app.models import ProjectCollaborator
from app.models.notes import ProjectNote
from app.services import note_service, project_lifecycle
from tests.test_collaboration_access_postgresql import _assert_project_row_locked, _setup, _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


@pytest.mark.parametrize("revoke", [True, False])
async def test_note_write_rechecks_membership_after_project_lock(postgres_collaboration_apps, revoke):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        note = await owner.post(f"{base}/notes", json={"title": "原笔记", "content": "原内容"})
        assert note.status_code == 201, note.text
        path = f"{base}/notes/{note.json()['id']}"
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                await project_lifecycle.lock_active(holder, project["id"])
                member = await holder.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
                ))
                if revoke:
                    member.status = "revoked"
                else:
                    member.role = "viewer"
                member.revision += 1
                await holder.flush()
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                pending = asyncio.create_task(guest.patch(path, json={
                    "expected_revision": 1, "content": "撤权后的写入",
                }))
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                async with guest_app.state.sessionmaker() as db:
                    blocked = (await db.scalars(text(
                        "SELECT query FROM pg_stat_activity WHERE datname=current_database() "
                        "AND :holder=ANY(pg_blocking_pids(pid))"
                    ), {"holder": pid})).all()
                    assert any("update projects" in query.lower() for query in blocked), blocked
                await holder.commit()
            result = await asyncio.wait_for(pending, 6)
            assert result.status_code == (404 if revoke else 403), result.text
            async with owner_app.state.sessionmaker() as db:
                saved = await db.get(ProjectNote, note.json()["id"])
                assert (saved.content, saved.revision) == ("原内容", 1)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_note_write_commits_before_waiting_revoke(postgres_collaboration_apps, monkeypatch):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        note = await owner.post(f"{base}/notes", json={"title": "原笔记", "content": "原内容"})
        assert note.status_code == 201, note.text
        path = f"{base}/notes/{note.json()['id']}"
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = note_service.require_project

        async def gated(db, actor_id, project_id, capability, **kwargs):
            result = await original(db, actor_id, project_id, capability, **kwargs)
            if actor_id == user["id"] and capability == "content.edit" and kwargs.get("for_update"):
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return result

        monkeypatch.setattr(note_service, "require_project", gated)
        write = asyncio.create_task(guest.patch(path, json={"expected_revision": 1, "content": "先提交的写入"}))
        revoke = None
        try:
            await asyncio.wait_for(reached.wait(), 6)
            await _assert_project_row_locked(owner_app.state.sessionmaker, project["id"])
            revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{user['id']}", params={
                "expected_revision": 2,
            }))
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 6)
            release.set()
            assert (await asyncio.wait_for(write, 6)).status_code == 200
            assert (await asyncio.wait_for(revoke, 6)).status_code == 204
            async with owner_app.state.sessionmaker() as db:
                saved = await db.get(ProjectNote, note.json()["id"])
                assert (saved.content, saved.revision) == ("先提交的写入", 2)
            assert (await guest.get(path)).status_code == 404
        finally:
            release.set()
            for task in (write, revoke):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


async def test_analysis_writeback_locks_only_its_own_child_on_postgresql(postgres_collaboration_apps, fake_llm):
    from app.models import AnalysisChange, ChapterAnalysis

    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, _, base = await _setup(owner, guest)
        chapter = await owner.post(f"{base}/chapters", json={"title": "章节", "chapter_no": 1})
        assert chapter.status_code == 201
        async with owner_app.state.sessionmaker() as db:
            analysis = ChapterAnalysis(project_id=project["id"], chapter_id=chapter.json()["id"], chapter_no=1,
                                       content_hash="0" * 64)
            db.add(analysis)
            await db.flush()
            change = AnalysisChange(project_id=project["id"], analysis_id=analysis.id, chapter_no=1,
                                    kind="foreshadow_new", status="pending", after={"title": "伏笔"})
            db.add(change)
            await db.flush()
            change_id = change.id
            await db.commit()
        confirmed = await guest.post(f"/api/v1/analysis-changes/{change_id}/confirm")
        assert confirmed.status_code == 200, confirmed.text
        assert (await guest.post(f"/api/v1/analysis-changes/{change_id}/revert")).status_code == 200
        assert not fake_llm.calls and not fake_llm.embed_calls


@pytest.mark.parametrize("revoke", [True, False])
async def test_analysis_writeback_rechecks_membership_after_project_lock(postgres_collaboration_apps, revoke, fake_llm):
    from sqlalchemy import func

    from app.models import AnalysisChange, ChapterAnalysis, Foreshadow

    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        chapter = await owner.post(f"{base}/chapters", json={"title": "章节", "chapter_no": 1})
        assert chapter.status_code == 201
        async with owner_app.state.sessionmaker() as db:
            analysis = ChapterAnalysis(project_id=project["id"], chapter_id=chapter.json()["id"], chapter_no=1,
                                       content_hash="0" * 64)
            db.add(analysis)
            await db.flush()
            change = AnalysisChange(project_id=project["id"], analysis_id=analysis.id, chapter_no=1,
                                    kind="foreshadow_new", status="pending", after={"title": "不应写入"})
            db.add(change)
            await db.flush()
            change_id = change.id
            await db.commit()
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                await project_lifecycle.lock_active(holder, project["id"])
                member = await holder.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
                ))
                if revoke:
                    member.status = "revoked"
                else:
                    member.role = "viewer"
                member.revision += 1
                await holder.flush()
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                pending = asyncio.create_task(guest.post(f"/api/v1/analysis-changes/{change_id}/confirm"))
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                await holder.commit()
            result = await asyncio.wait_for(pending, 6)
            assert result.status_code == (404 if revoke else 403), result.text
            async with owner_app.state.sessionmaker() as db:
                assert (await db.get(AnalysisChange, change_id)).status == "pending"
                assert await db.scalar(select(func.count()).select_from(Foreshadow).where(
                    Foreshadow.project_id == project["id"],
                )) == 0
            assert not fake_llm.calls and not fake_llm.embed_calls
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


@pytest.mark.parametrize(("entrypoint", "relation"), [
    ("import", "plan"), ("import", "chapter"), ("import", "unit"), ("sync", "chapter"), ("sync", "unit"),
])
async def test_outline_mutation_rolls_back_foreign_references_on_postgresql(
    postgres_collaboration_apps, entrypoint, relation,
):
    from sqlalchemy import func

    from app.models import Chapter, ChapterPlan, Outline, OutlineSnapshot
    from app.models.planning import PlanningUnit
    from tests.helpers import create_project

    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, _, base = await _setup(owner, guest)
        foreign = await create_project(owner, title="另一本")
        outline = await owner.post(f"{base}/outlines", json={"title": "原大纲"})
        assert outline.status_code == 201
        outline_id = outline.json()["id"]
        result = await owner.put(f"{base}/outlines/{outline_id}/plans", json={"plans": [{"title": "原细纲"}]})
        assert result.status_code == 200
        plan_id = result.json()[0]["id"]
        async with owner_app.state.sessionmaker() as db:
            if relation == "plan":
                foreign_row = ChapterPlan(project_id=foreign["id"], outline_id=outline_id, seq=2, title="别书细纲")
            elif relation == "chapter":
                foreign_row = Chapter(project_id=foreign["id"], chapter_no=1, title="别书章", plan_id=plan_id)
            else:
                foreign_row = PlanningUnit(project_id=foreign["id"], plan_id=plan_id)
            db.add(foreign_row)
            await db.flush()
            foreign_id, model = foreign_row.id, type(foreign_row)
            await db.commit()
        body = {"mode": "replace", "data": {"format": "ainovel.outline", "version": 1,
                                           "volumes": [{"outlines": [{"title": "替换"}]}]}}
        response = (await guest.post(f"{base}/outlines/import", json=body) if entrypoint == "import"
                    else await guest.put(f"{base}/outlines/{outline_id}/plans", json={"plans": []}))
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "outline_reference_conflict"
        async with owner_app.state.sessionmaker() as db:
            assert (await db.get(Outline, outline_id)).title == "原大纲"
            assert (await db.get(ChapterPlan, plan_id)).title == "原细纲"
            saved = await db.get(model, foreign_id)
            assert saved is not None
            assert (saved.outline_id if relation == "plan" else saved.plan_id) == (
                outline_id if relation == "plan" else plan_id
            )
            assert await db.scalar(select(func.count()).select_from(OutlineSnapshot).where(
                OutlineSnapshot.project_id == project["id"],
            )) == 0


async def test_plan_sync_preserves_foreign_chapter_references_on_postgresql(postgres_collaboration_apps):
    from sqlalchemy import func

    from app.models import Chapter, ChapterPlan, OutlineSnapshot
    from app.models.planning import PlanningUnit
    from tests.helpers import create_project

    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, _, base = await _setup(owner, guest)
        foreign = await create_project(owner, title="另一本")
        outline = await owner.post(f"{base}/outlines", json={"title": "原大纲"})
        assert outline.status_code == 201
        path = f"{base}/outlines/{outline.json()['id']}/plans"
        plans = await owner.put(path, json={"plans": [{"title": "原细纲"}]})
        assert plans.status_code == 200
        built = await owner.post(f"{path}/build")
        assert built.status_code == 200
        chapter_id = built.json()["chapters"][0]["id"]
        async with owner_app.state.sessionmaker() as db:
            foreign_unit = PlanningUnit(project_id=foreign["id"], chapter_id=chapter_id)
            db.add(foreign_unit)
            await db.flush()
            foreign_unit_id = foreign_unit.id
            await db.commit()
        response = await guest.put(path, json={"plans": []})
        assert response.status_code == 409, response.text
        async with owner_app.state.sessionmaker() as db:
            assert (await db.get(ChapterPlan, plans.json()[0]["id"])).chapter_id == chapter_id
            assert (await db.get(PlanningUnit, foreign_unit_id)).chapter_id == chapter_id
            assert await db.get(Chapter, chapter_id) is not None
            assert await db.scalar(select(func.count()).select_from(OutlineSnapshot).where(
                OutlineSnapshot.project_id == project["id"],
            )) == 0


async def test_scene_planning_rejects_foreign_pov_on_postgresql(postgres_collaboration_apps):
    from app.core.errors import ConflictError
    from app.models.planning import PlanningUnit
    from app.services.planning_scene_service import resolve_scene_plannings
    from tests.helpers import create_project

    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, _, base = await _setup(owner, guest)
        chapter = await owner.post(f"{base}/chapters", json={"title": "章节", "chapter_no": 1})
        assert chapter.status_code == 201
        scenes_url = f"{base}/chapters/{chapter.json()['id']}/scenes"
        scene_id = (await owner.get(scenes_url)).json()["items"][0]["id"]
        foreign = await create_project(owner, title="另一本")
        character = await owner.post(f"/api/v1/projects/{foreign['id']}/characters", json={"name": "别书角色"})
        assert character.status_code == 201
        local = await owner.post(f"{base}/characters", json={"name": "本书角色"})
        assert local.status_code == 201
        async with owner_app.state.sessionmaker() as db:
            unit = PlanningUnit(project_id=project["id"], scene_id=scene_id, source_level="scene", pov_mode="override",
                                pov_character_id=character.json()["id"])
            db.add(unit)
            await db.flush()
            unit_id = unit.id
            await db.commit()
        response = await guest.get(f"{scenes_url}/{scene_id}")
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "scene_reference_invalid"
        async with owner_app.state.sessionmaker() as db:
            (await db.get(PlanningUnit, unit_id)).pov_character_id = None
            await db.commit()
        async with guest_app.state.sessionmaker() as db:
            unit = await db.get(PlanningUnit, unit_id)
            unit.pov_character_id = character.json()["id"]
            with pytest.raises(ConflictError):
                await resolve_scene_plannings(db, project["id"], [scene_id])
            assert unit.pov_character_id == character.json()["id"] and unit in db.dirty
            await db.rollback()
        async with guest_app.state.sessionmaker() as db:
            unit = await db.get(PlanningUnit, unit_id)
            unit.pov_character_id = local.json()["id"]
            result = await resolve_scene_plannings(db, project["id"], [scene_id])
            assert result[scene_id].raw.pov_character_id == local.json()["id"] and unit in db.dirty
            await db.rollback()
