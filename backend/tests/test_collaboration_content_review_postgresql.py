"""隔离 PostgreSQL 上验证章节删除与其他人的任务持锁提交顺序。"""

import asyncio
from contextlib import suppress
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text

from app.models import Chapter, ChapterPlan, Outline, PlanningUnit, Scene, SceneMarker, Task
from app.services import project_lifecycle
from tests.helpers import create_project
from tests.test_collaboration_access_postgresql import _setup, _wait_for_blocker
from tests.test_collaboration_rowlock_postgresql import _assert_blocked_on_project_row

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def test_other_task_commits_before_editor_chapter_delete(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, _, base = await _setup(owner, guest)
        owner_id = (await owner.get("/api/v1/auth/me")).json()["id"]
        response = await owner.post(f"{base}/chapters", json={"title": "待分析章节", "content": "正文"})
        assert response.status_code == 201, response.text
        chapter_id = response.json()["id"]
        deletion = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                await project_lifecycle.lock_active(holder, project["id"])
                task = Task(user_id=owner_id, project_id=project["id"], chapter_id=chapter_id,
                            kind="chapter.analyze", status="queued", input={"chapter_id": chapter_id})
                holder.add(task)
                await holder.flush()
                task_id = task.id
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                deletion = asyncio.create_task(guest.delete(f"{base}/chapters/{chapter_id}"))
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                await _assert_blocked_on_project_row(guest_app.state.sessionmaker, pid)
                await holder.commit()
            result = await asyncio.wait_for(deletion, 6)
            assert result.status_code == 409 and result.json()["code"] == "chapter_in_use", result.text
            async with owner_app.state.sessionmaker() as db:
                assert await db.get(Chapter, chapter_id) is not None
                saved = await db.get(Task, task_id)
                assert saved.chapter_id == chapter_id and saved.status == "queued"
        finally:
            if deletion is not None and not deletion.done():
                deletion.cancel()
                with suppress(asyncio.CancelledError):
                    await deletion


@pytest.mark.parametrize("reference", ["plan", "scene", "marker", "task", "valid"])
async def test_postgres_chapter_cascade_respects_project_ownership(postgres_collaboration_apps, reference):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, _, base = await _setup(owner, guest)
        other = await create_project(owner, title="另一书")
        owner_id = (await owner.get("/api/v1/auth/me")).json()["id"]
        first = await owner.post(f"{base}/chapters", json={"title": "第一章", "content": "正文"})
        second = await owner.post(f"/api/v1/projects/{other['id']}/chapters", json={"title": "他书章节"})
        assert first.status_code == second.status_code == 201
        chapter_id = first.json()["id"]
        async with owner_app.state.sessionmaker() as db:
            scene_id = await db.scalar(select(Scene.id).where(Scene.chapter_id == chapter_id))
            other_scene = await db.scalar(select(Scene.id).where(Scene.chapter_id == second.json()["id"]))
            if reference == "plan":
                outline = Outline(project_id=other["id"], chapter_no=1, title="他书大纲")
                db.add(outline)
                await db.flush()
                db.add(ChapterPlan(project_id=other["id"], outline_id=outline.id, seq=1, title="他书细纲",
                                   chapter_id=chapter_id))
            elif reference == "scene":
                db.add(PlanningUnit(project_id=other["id"], scene_id=scene_id, source_level="scene"))
            elif reference == "marker":
                origin = SceneMarker(scene_id=scene_id, logical_key=str(uuid4()), status="deleted", color="yellow")
                db.add(origin)
                await db.flush()
                db.add(SceneMarker(scene_id=other_scene, logical_key=str(uuid4()), status="deleted", color="yellow",
                                   origin_marker_id=origin.id))
            else:
                db.add(Task(user_id=owner_id, project_id=project["id"] if reference == "valid" else other["id"],
                            chapter_id=chapter_id, kind="chapter.analyze", status="succeeded"))
            await db.commit()
        response = await guest.delete(f"{base}/chapters/{chapter_id}")
        assert response.status_code == (204 if reference == "valid" else 409), response.text
        if reference != "valid":
            assert response.json()["code"] == "chapter_reference_invalid"
        async with owner_app.state.sessionmaker() as db:
            assert (await db.get(Chapter, chapter_id) is not None) == (reference != "valid")


@pytest.mark.parametrize('kind', ['foreign_pov', 'foreign_section', 'valid'])
async def test_postgres_version_snapshot_isolation(postgres_collaboration_apps, kind):
    from copy import deepcopy

    from app.models import ChapterVersion, Character, SceneSection
    from app.schemas.planning import ScenePlanningRaw
    from app.schemas.rich_document import DocumentSection, SceneDocument

    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url='http://testserver') as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url='http://testserver') as guest,
    ):
        project, _, base = await _setup(owner, guest)
        other = await create_project(owner, title='他书')
        created = await owner.post(f'{base}/chapters', json={'title': '本书', 'content': '正文'})
        assert created.status_code == 201, created.text
        chapter_id = created.json()['id']
        url = f'{base}/chapters/{chapter_id}'
        async with owner_app.state.sessionmaker() as db:
            scene_id = await db.scalar(select(Scene.id).where(Scene.chapter_id == chapter_id))
            unit = PlanningUnit(project_id=project['id'], scene_id=scene_id, source_level='scene')
            character = Character(project_id=other['id'] if kind == 'foreign_pov' else project['id'], name='角色')
            db.add_all([unit, character])
            await db.flush()
            version = await db.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_id)
                                      .order_by(ChapterVersion.seq.desc()).limit(1))
            snapshot = deepcopy(version.scene_snapshot)
            item = snapshot['scenes'][0]
            raw = ScenePlanningRaw().model_dump(mode='json')
            raw['modes']['pov'] = 'override'
            raw['pov_character_id'] = character.id
            item['planning'] = {'unit_id': unit.id, 'binding_epoch': 0, 'raw': raw}
            if kind == 'foreign_section':
                foreign_chapter = Chapter(project_id=other['id'], chapter_no=1, title='他书章')
                db.add(foreign_chapter)
                await db.flush()
                foreign_scene = Scene(chapter_id=foreign_chapter.id, title='他书景', content='密文', summary='',
                                      sort=0, separator_before='', active=True)
                db.add(foreign_scene)
                await db.flush()
                key = uuid4()
                db.add(SceneSection(scene_id=foreign_scene.id, logical_key=str(key), anchor=0, sort=0,
                                    title='他书区块', content='密文'))
                document = SceneDocument(content=item['content'], sections=[
                    DocumentSection(key=key, anchor=0, order=0, title='他书区块', content='密文'),
                ])
                snapshot['schema_version'] = 2
                item['document'] = document.model_dump(mode='json', exclude={'content'})
            version.scene_snapshot = snapshot
            version_id = version.id
            await db.commit()
        endpoint = f'{url}/versions/{version_id}'
        detail = await guest.get(endpoint)
        restored = await guest.post(f'{endpoint}/restore', json={'expected_content_revision': 0})
        if kind == 'valid':
            assert detail.status_code == 200 and detail.json()['scene_snapshot'] == snapshot
            assert restored.status_code == 200, restored.text
        else:
            for result in (detail, restored):
                assert result.status_code == 409, result.text
                assert result.json() == {
                    'detail': '场景历史关联数据不一致，无法操作', 'code': 'scene_reference_invalid',
                }
        async with owner_app.state.sessionmaker() as db:
            assert (await db.get(Chapter, chapter_id)).content_revision == (1 if kind == 'valid' else 0)
