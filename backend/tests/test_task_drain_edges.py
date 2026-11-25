"""排空期间，附带事务写入的任务入口和备份计划的 HTTP 回归。"""

import asyncio
import shutil
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import AgentChange, BackupRun, BackupSettings, BookImport, Chapter, Project, SceneReviewProposal, Task
from app.schemas.scene_ai import ChapterTarget
from app.services import kb_parser, task_service
from app.services.agent.tools import tasks as tasks_tool
from app.services.system_settings_snapshot import SettingsUnavailable
from tests.helpers import run_task, task_params, wait_task
from tests.schema_helpers import isolated_worker_database
from tests.test_agent_derived_task_admission import _propose
from tests.test_agent_runtime import call, change_action, send_task, setup_agent, turn_action
from tests.test_backup_runtime import _accept, _finished
from tests.test_batch_generate import _setup as setup_batch
from tests.test_batch_generate import _start as start_batch
from tests.test_book_import import _setup as setup_import
from tests.test_book_import import _start as start_import
from tests.test_book_import_skip_identity import _paused_task
from tests.test_c5_backup import _configure
from tests.test_collaboration_review_claim import _claimed_proposal_world, _persist
from tests.test_kb import _doc
from tests.test_kb_editor_entries_e6c import _world
from tests.test_rich_document_api import KEY, add_section, save_payload
from tests.test_system_settings import save_settings

MAINTENANCE = {"code": "task_admission_closed", "detail": "系统正在维护，暂不受理新任务，请稍后再试"}


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


def _assert_maintenance(response):
    assert response.status_code == 503, response.text
    assert response.json() == MAINTENANCE


async def _lose_settings_connection(db):
    connection = await db.connection()
    await connection.invalidate()
    raise SettingsUnavailable()


@pytest.mark.parametrize("after_preflight", [False, True])
async def test_agent_task_confirmation_retains_proposal_and_chapter(
    app, client, fake_llm, system_admin, monkeypatch, after_preflight,
):
    project, base, _, chapter_id, view, turn = await _propose(
        app, client, fake_llm, "chapter.generate", lambda cid: {"chapter_id": cid},
    )
    if after_preflight:
        prepare = tasks_tool.prepare_confirmed_admission

        async def close_after_preparing(*args, **kwargs):
            result = await prepare(*args, **kwargs)
            assert result is not None and result.admission is not None
            await save_settings(system_admin, task_admission_closed=True)
            return result

        monkeypatch.setattr(tasks_tool, "prepare_confirmed_admission", close_after_preparing)
    else:
        await save_settings(system_admin, task_admission_closed=True)
    before = len(fake_llm.tool_calls), len(fake_llm.calls)
    response = await client.post(f"{base}/changes/{view['id']}/confirm", json=change_action(view))
    _assert_maintenance(response)
    assert (len(fake_llm.tool_calls), len(fake_llm.calls)) == before
    async with app.state.sessionmaker() as db:
        proposal = await db.get(AgentChange, view["id"])
        chapter = await db.get(Chapter, chapter_id)
        assert proposal.status == "proposed" and set(proposal.result) == {"_agent_task_origin"}
        assert (await db.get(Task, turn["id"])).status == "paused"
        assert await db.scalar(select(func.count(Task.id)).where(Task.project_id == project["id"])) == 1
        assert chapter.status == "draft" and chapter.generation_claim_key is None


async def test_agent_keeps_saved_decisions_and_allows_manual_resume_after_reopening(client, fake_llm, system_admin):
    project, base, url = await setup_agent(client)
    fake_llm.push_tools([call("create_character", {"name": "林远"})])
    turn = await send_task(client, url)
    await wait_task(client, turn, statuses=("paused",))
    await save_settings(system_admin, task_admission_closed=True)
    calls_before = len(fake_llm.calls)

    response = await client.post(
        f"{base}/turns/{turn['id']}/confirm-all", json=await turn_action(client, url, turn, "confirm-all"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["notice"] == MAINTENANCE["detail"]
    assert response.json()["task"]["status"] == "paused"
    assert [row["status"] for row in response.json()["changes"]] == ["executed"]
    characters = f"/api/v1/projects/{project['id']}/characters"
    assert [row["name"] for row in (await client.get(characters)).json()] == ["林远"]
    assert len(fake_llm.calls) == calls_before

    await save_settings(system_admin, task_admission_closed=False)
    fake_llm.push_tools([("text", {"text": "角色已创建"})])
    resumed = await client.post(f"/api/v1/tasks/{turn['id']}/resume", params=task_params(turn))
    assert resumed.status_code == 202, resumed.text
    assert (await wait_task(client, turn))["status"] == "succeeded"
    assert [row["name"] for row in (await client.get(characters)).json()] == ["林远"]


@pytest.mark.parametrize("settings_unavailable", [False, True], ids=["draining", "settings-read-failed"])
async def test_agent_bulk_reports_committed_decisions_before_rejected_task(
    client, fake_llm, system_admin, monkeypatch, settings_unavailable,
):
    project, base, url = await setup_agent(client)
    project_url = f"/api/v1/projects/{project['id']}"
    chapter = await client.post(project_url + "/chapters", json={"title": "首章", "content": "初稿"})
    assert chapter.status_code == 201
    fake_llm.push_tools([
        call("create_character", {"name": "林远"}),
        call("start_task", {"kind": "chapter.generate", "params": {"chapter_id": chapter.json()["id"]}}, "c2"),
    ])
    turn = await send_task(client, url)
    await wait_task(client, turn, statuses=("paused",))
    if settings_unavailable:
        monkeypatch.setattr(tasks_tool, "require_task_admission_open", _lose_settings_connection)
        reason = SettingsUnavailable().detail
    else:
        await save_settings(system_admin, task_admission_closed=True)
        reason = MAINTENANCE["detail"]
    calls_before = len(fake_llm.calls)

    response = await client.post(
        f"{base}/turns/{turn['id']}/confirm-all", json=await turn_action(client, url, turn, "confirm-all"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["error"] == reason
    assert response.json()["task"]["status"] == "paused"
    assert [row["status"] for row in response.json()["changes"]] == ["executed", "proposed"]
    assert [row["name"] for row in (await client.get(project_url + "/characters")).json()] == ["林远"]
    assert len(fake_llm.calls) == calls_before


async def test_agent_reports_saved_decisions_after_failed_settings_read(client, fake_llm, monkeypatch):
    project, base, url = await setup_agent(client)
    fake_llm.push_tools([call("create_character", {"name": "林远"})])
    turn = await send_task(client, url)
    await wait_task(client, turn, statuses=("paused",))

    monkeypatch.setattr(task_service, "require_task_admission_open", _lose_settings_connection)
    response = await client.post(
        f"{base}/turns/{turn['id']}/confirm-all", json=await turn_action(client, url, turn, "confirm-all"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["notice"] == SettingsUnavailable().detail
    assert response.json()["task"]["status"] == "paused"
    assert [row["status"] for row in response.json()["changes"]] == ["executed"]
    characters = f"/api/v1/projects/{project['id']}/characters"
    assert [row["name"] for row in (await client.get(characters)).json()] == ["林远"]


async def test_rejected_book_import_retains_parsed_file_and_creates_no_project(app, client, fake_llm, system_admin):
    uploaded = await setup_import(client)
    await save_settings(system_admin, task_admission_closed=True)
    _assert_maintenance(await start_import(client, uploaded))
    async with app.state.sessionmaker() as db:
        record = await db.get(BookImport, uploaded["id"])
        assert record.revision == uploaded["revision"] and len(record.chapters) == uploaded["chapter_count"]
        assert await db.scalar(select(func.count(Project.id))) == 0
        assert await db.scalar(select(func.count(Task.id))) == 0
    assert not fake_llm.calls


async def test_book_import_skip_retains_original_checkpoint(app, client, fake_llm, system_admin):
    _, _, task_id, task_key, _, checkpoint = await _paused_task(app, client)
    await save_settings(system_admin, task_admission_closed=True)
    _assert_maintenance(await client.post(f"/api/v1/book-imports/tasks/{task_id}/skip", params={"task_key": task_key}))
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        assert task.status == "paused" and task.checkpoint == checkpoint and task.step_cursor == 0
    assert not fake_llm.calls


async def test_rich_batch_accept_keeps_review_and_original_document(app, client, fake_llm, system_admin):
    _, base, (chapter, next_chapter), _ = await setup_batch(client, ["原稿", "下一章"])
    url = f"{base}/chapters/{chapter['id']}"
    scene = (await client.get(f"{url}/scenes")).json()["items"][0]
    document = f"{url}/scenes/{scene['id']}/document"
    draft = save_payload((await client.get(document)).json())
    add_section(draft)
    saved = await client.put(document, json=draft)
    assert saved.status_code == 200, saved.text
    fake_llm.push("完整的候选正文")
    task, events = await run_task(client, await start_batch(client, base, chapter, next_chapter, include_written=True))
    assert task["status"] == "paused"
    identity = events[-1][1]["result"]
    async with app.state.sessionmaker() as db:
        original = await db.get(Task, task["id"])
        checkpoint, queued_at = deepcopy(original.checkpoint), original.queued_at

    await save_settings(system_admin, task_admission_closed=True)
    path = f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}/accept"
    _assert_maintenance(await client.post(path, json={
        "task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 1}],
        "confirm_format_loss": False,
    }))
    assert (await client.get(document)).json() == saved.json()
    assert len(fake_llm.calls) == 1
    async with app.state.sessionmaker() as db:
        original = await db.get(Task, task["id"])
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        assert (original.status, original.checkpoint, original.queued_at) == ("paused", checkpoint, queued_at)
        assert proposal.status == "pending"


async def test_single_rich_review_keeps_manual_result_and_reports_skipped_analysis(app, client, system_admin):
    job, plans, logs, document, source, scene = await _claimed_proposal_world(app, client)
    job.target = ChapterTarget()
    identity = await _persist(app, job, plans, logs, pause=True)
    await save_settings(system_admin, task_admission_closed=True, auto_analyze_enabled=True)

    path = f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept"
    response = await client.post(path, json={
        "task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 2}],
        "confirm_format_loss": True,
    })
    assert response.status_code == 200, response.text
    assert response.json()["analysis_warning"] == "系统维护期间未排队自动分析，请稍后手动分析"
    assert (await client.get(document)).json()["document"]["content"] != source["document"]["content"]
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, job.task_id)).status == "succeeded"
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "chapter.analyze")) == 0


async def test_kb_rejections_preserve_index_and_allow_manual_memory(
    app, client, make_client, fake_llm, system_admin, monkeypatch,
):
    editor = make_client()
    world = await _world(app, client, editor)
    base = world["base"]
    doc_id = world["doc"]
    before = await _doc(client, base + "/kb", doc_id)
    chunks_path = f"{base}/kb/documents/{doc_id}/chunks"
    chunks = (await client.get(chunks_path)).json()
    await save_settings(system_admin, task_admission_closed=True)

    def unexpected_parse(*args, **kwargs):
        raise AssertionError("排空时无需解析用于后台索引的新文件")

    monkeypatch.setattr(kb_parser, "parse", unexpected_parse)
    for actor in (client, editor):
        _assert_maintenance(await actor.post(f"{base}/kb/documents", files={
            "file": ("新资料.txt", "待索引资料".encode(), "text/plain"),
        }))
        _assert_maintenance(await actor.post(f"{base}/kb/documents/{doc_id}/reindex"))
        _assert_maintenance(await actor.post(f"{base}/kb/rebuild"))
        memory = await actor.post(f"{base}/memories", json={"chapter_no": 1, "content": "可保存的记忆"})
        assert memory.status_code == 201, memory.text
    assert (await client.get(f"{base}/kb/documents")).json() == [before]
    assert (await client.get(chunks_path)).json() == chunks
    assert not fake_llm.embed_calls
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.project_id == world["project"])) == 0


async def test_drain_retains_due_schedule_and_runs_existing_and_manual_backups(
    app, second_app, system_admin, monkeypatch,
):
    if app.state.engine.dialect.name == "postgresql" and shutil.which(app.state.settings.backup_pg_dump_path) is None:
        pytest.skip("真实 PostgreSQL 备份需要路径上的 pg_dump")
    await _configure(system_admin)
    first, peer = app.state.backups, second_app.state.backups
    due = datetime.now(UTC).replace(hour=4, minute=0, second=0, microsecond=0) + timedelta(days=1)
    async with app.state.sessionmaker() as db:
        config = await db.get(BackupSettings, 1)
        config.configuration = {**config.configuration, "schedule_enabled": True}
        config.next_scheduled_at = due
        await db.commit()
    runtime = app.state.backup_runtime
    release = asyncio.Event()
    execute = runtime._execute

    async def wait_for_drain(run_id):
        await release.wait()
        return await execute(run_id)

    monkeypatch.setattr(runtime, "_execute", wait_for_drain)
    await runtime.start()
    try:
        existing = await _accept(system_admin)
        await save_settings(system_admin, task_admission_closed=True)
        release.set()
        assert (await _finished(app, existing)).status == "succeeded"
        assert await asyncio.gather(first.accept_scheduled(due), peer.accept_scheduled(due)) == [None, None]
        async with app.state.sessionmaker() as db:
            config = await db.get(BackupSettings, 1)
            assert config.next_scheduled_at.replace(tzinfo=UTC) == due
            assert list(await db.scalars(select(BackupRun.id))) == [existing]
        manual = await _accept(system_admin)
        assert (await _finished(app, manual)).status == "succeeded"

        await save_settings(system_admin, task_admission_closed=False)
        accepted = await asyncio.gather(first.accept_scheduled(due), peer.accept_scheduled(due))
        assert sum(run is not None for run in accepted) == 1
        async with app.state.sessionmaker() as db:
            scheduled = list(await db.scalars(select(BackupRun.scheduled_date).where(BackupRun.source == "scheduled")))
            assert scheduled == [due.date().isoformat()]
    finally:
        release.set()
        await runtime.stop()
