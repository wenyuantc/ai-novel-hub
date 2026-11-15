"""分析入口在完成协作权限检查前不得刷新调用方的待写数据。"""

import pytest
from sqlalchemy import event

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models import AnalysisChange, Chapter, ChapterAnalysis
from app.services import analysis_service, writeback_service
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role


async def test_denied_analysis_and_writeback_never_flush_pending_entities(app, client, make_client, system_admin):
    await register(client, "analysis-flush-owner")
    viewer_client, admin_client, stranger_client = (make_client() for _ in range(3))
    viewer = await register(viewer_client, "analysis-flush-viewer")
    admin = await register(admin_client, "analysis-flush-admin")
    stranger = await register(stranger_client, "analysis-flush-stranger")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    invitation = await client.post(f"{base}/collaborators", json={
        "username": "analysis-flush-viewer", "role": "viewer",
    })
    assert invitation.status_code == 201
    accept_url = f"/api/v1/collaboration/invitations/project/{invitation.json()['invitation_id']}/accept"
    assert (await viewer_client.post(accept_url, json={"expected_revision": 1})).status_code == 200
    role = await create_role(system_admin, "分析只读管理员", ["projects.read", "projects.edit"])
    await assign_roles(system_admin, admin["id"], [role["id"]])
    chapter = await client.post(f"{base}/chapters", json={"title": "章节", "chapter_no": 1})
    assert chapter.status_code == 201
    chapter_id = chapter.json()["id"]
    async with app.state.sessionmaker() as db:
        analysis = ChapterAnalysis(project_id=project["id"], chapter_id=chapter_id, chapter_no=1,
                                   content_hash="0" * 64)
        db.add(analysis)
        await db.flush()
        change = AnalysisChange(project_id=project["id"], analysis_id=analysis.id, chapter_no=1,
                                kind="foreshadow_new", status="pending", after={"title": "伏笔"})
        db.add(change)
        await db.flush()
        analysis_id, change_id = analysis.id, change.id
        await db.commit()

    cases = [
        (analysis_service.chapter_state, chapter_id, stranger["id"], NotFoundError, {}),
        (analysis_service.chapter_state, chapter_id, admin["id"], NotFoundError, {}),
        (analysis_service.list_analyses, chapter_id, stranger["id"], NotFoundError, {}),
        (analysis_service.list_analyses, chapter_id, admin["id"], NotFoundError, {}),
        (analysis_service.get_analysis, analysis_id, stranger["id"], NotFoundError, {"manual": True}),
        (analysis_service.get_analysis, analysis_id, admin["id"], NotFoundError, {"manual": True}),
        (writeback_service.get_manual_change, change_id, stranger["id"], NotFoundError, {}),
        (writeback_service.get_manual_change, change_id, admin["id"], NotFoundError, {}),
        (writeback_service.get_manual_change, change_id, viewer["id"], ForbiddenError, {}),
    ]
    for service, resource_id, actor_id, error, kwargs in cases:
        async with app.state.sessionmaker() as db:
            pending = Chapter(project_id=project["id"], title="不得提前写入")
            db.add(pending)
            flushes = []
            event.listen(db.sync_session, "before_flush", lambda *_, marks=flushes: marks.append(True))
            with pytest.raises(error):
                await service(db, actor_id, resource_id, **kwargs)
            assert flushes == [], service.__name__
            assert pending.id is None and pending in db.new
            await db.rollback()


async def test_writeback_validates_child_before_flush_and_preserves_pending_target(app, client):
    owner = await register(client, "writeback-flush-owner")
    project = await create_project(client)
    foreign = await create_project(client, title="另一本")
    async with app.state.sessionmaker() as db:
        chapters = [Chapter(project_id=pid, chapter_no=1, title="章节") for pid in (project["id"], foreign["id"])]
        db.add_all(chapters)
        await db.flush()
        analyses = [ChapterAnalysis(project_id=chapter.project_id, chapter_id=chapter.id, chapter_no=1,
                                   content_hash="0" * 64) for chapter in chapters]
        db.add_all(analyses)
        await db.flush()
        changes = [AnalysisChange(project_id=project["id"], analysis_id=analysis.id, chapter_no=1,
                                  kind="foreshadow_new", status="pending", after={"title": "伏笔"})
                   for analysis in analyses]
        db.add_all(changes)
        await db.flush()
        valid_id, inconsistent_id = (change.id for change in changes)
        await db.commit()

    async with app.state.sessionmaker() as db:
        pending = Chapter(project_id=project["id"], title="不得提前写入")
        db.add(pending)
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        with pytest.raises(NotFoundError):
            await writeback_service.get_manual_change(db, owner["id"], inconsistent_id)
        assert flushes == [] and pending.id is None and pending in db.new
        await db.rollback()

    async with app.state.sessionmaker() as db:
        change = await db.get(AnalysisChange, valid_id)
        change.status = "rejected"
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        with pytest.raises(ConflictError):
            await writeback_service.get_manual_change(db, owner["id"], valid_id)
        assert flushes == [] and change.status == "rejected" and change in db.dirty
        await db.rollback()


async def test_analysis_validates_chapter_before_flush_and_preserves_pending_target(app, client):
    owner = await register(client, "analysis-child-flush-owner")
    project = await create_project(client)
    foreign = await create_project(client, title="另一本")
    async with app.state.sessionmaker() as db:
        chapters = [Chapter(project_id=pid, chapter_no=1, title="章节") for pid in (project["id"], foreign["id"])]
        db.add_all(chapters)
        await db.flush()
        analyses = [ChapterAnalysis(project_id=project["id"], chapter_id=chapter.id, chapter_no=1,
                                   content_hash="0" * 64) for chapter in chapters]
        db.add_all(analyses)
        await db.flush()
        valid_id, inconsistent_id = (analysis.id for analysis in analyses)
        await db.commit()

    async with app.state.sessionmaker() as db:
        pending = Chapter(project_id=project["id"], title="不得提前写入")
        db.add(pending)
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        with pytest.raises(NotFoundError):
            await analysis_service.get_analysis(db, owner["id"], inconsistent_id, manual=True)
        assert flushes == [] and pending.id is None and pending in db.new
        await db.rollback()

    async with app.state.sessionmaker() as db:
        analysis = await db.get(ChapterAnalysis, valid_id)
        analysis.summary = "未提交的内容"
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        with pytest.raises(ConflictError):
            await analysis_service.get_analysis(db, owner["id"], valid_id, manual=True)
        assert flushes == [] and analysis.summary == "未提交的内容" and analysis in db.dirty
        await db.rollback()
