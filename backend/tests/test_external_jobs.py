"""外部厂商仅在内存计数，数据库、执行身份、结果落盘及计量使用实际服务。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.ai import external_jobs as jobs
from app.ai.providers.video import VideoPoll, VideoTransientError
from app.core.errors import AppError
from app.models import AIConfig, Task
from app.models.ai_external_jobs import AiExternalJob
from app.models.ai_usage import AiUsageCall
from app.models.media import MediaObject
from app.services.task_engine.execution_identity import from_queued_task
from tests.helpers import register

VIDEO = b"\x00\x00\x00\x18ftypmp42" + bytes(64)


class FakeVideo:
    def __init__(self):
        self.submits = self.downloads = self.cancels = 0
        self.polls = [VideoPoll("succeeded", duration_seconds=4)]

    def validate(self, request):
        assert request.model == "test-video"

    async def submit(self, request):
        self.submits += 1
        return "vendor-1"

    async def poll(self, external_id):
        assert external_id == "vendor-1"
        outcome = self.polls.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def download(self, poll):
        self.downloads += 1
        yield VIDEO

    async def cancel(self, external_id):
        assert external_id == "vendor-1"
        self.cancels += 1


async def emit(stage, progress):
    assert stage and 0 <= progress <= 100


@pytest.fixture
async def video_job(app, client, monkeypatch):
    user_id = (await register(client))["id"]
    ctx = app.state.task_engine.ctx
    fake = FakeVideo()
    monkeypatch.setattr(jobs, "build_video_provider", lambda *args, **kwargs: fake)
    monkeypatch.setattr(jobs, "_POLL_INTERVAL", 0.001)
    async with ctx.sessionmaker() as db:
        cfg = AIConfig(name="test", user_id=user_id, provider="openai_compat", model="test-video",
                       model_type="video", api_key_enc=ctx.cipher.encrypt("test"))
        db.add(cfg)
        await db.flush()
        job_id = await jobs.create_job(db, ctx, actor_id=user_id, config=cfg,
                                       request=dict(prompt="test", aspect_ratio="16:9", resolution="720p",
                                                    duration_seconds=4))
        task = Task(user_id=user_id, kind="ai.video_probe",
                    input={"external_job_ids": [job_id], "_task_actor_token_version": 0})
        db.add(task)
        await db.flush()
        execution = from_queued_task(task, str(uuid4()), worker_key=ctx.task_engine.lease.worker_key)
        task.worker_key = execution.worker_key
        task.status = "running"
        task.execution_key = execution.execution_key
        await db.commit()
    return ctx, fake, job_id, execution


async def test_success_reentry_and_transient_poll(video_job):
    ctx, fake, job_id, execution = video_job
    fake.polls.insert(0, VideoTransientError("网络波动"))
    for _ in range(2):
        assert await jobs.drive(ctx, [job_id], execution=execution, emit=emit) == [job_id]
    assert (fake.submits, fake.downloads) == (1, 1)
    async with ctx.sessionmaker() as db:
        row = await db.get(AiExternalJob, job_id)
        assert (row.state, row.external_id, row.usage_call_id) == ("succeeded", "vendor-1", job_id)
        assert (await db.get(MediaObject, job_id)).status == "ready"
        assert await db.scalar(select(func.count()).select_from(AiUsageCall)) == 1


async def test_resume_submitted_and_uncertain_submit(video_job):
    ctx, fake, job_id, execution = video_job
    async with ctx.sessionmaker() as db:
        job = await db.get(AiExternalJob, job_id)
        job.state = "submitting"
        await db.commit()
    with pytest.raises(AppError, match="确认"):
        await jobs.drive(ctx, [job_id], execution=execution, emit=emit)
    assert fake.submits == 0
    async with ctx.sessionmaker() as db:
        job = await db.get(AiExternalJob, job_id)
        job.state, job.external_id = "submitted", "vendor-1"
        job.submitted_at = datetime.now(UTC)
        job.deadline_at = datetime.now(UTC) + timedelta(seconds=30)
        await db.commit()
    assert await jobs.drive(ctx, [job_id], execution=execution, emit=emit) == [job_id]
    assert fake.submits == 0


@pytest.mark.parametrize("outcome", ["timeout", "cancel", "failed"])
async def test_terminal_states(video_job, outcome):
    ctx, fake, job_id, execution = video_job
    async with ctx.sessionmaker() as db:
        job = await db.get(AiExternalJob, job_id)
        now = datetime.now(UTC)
        job.state, job.external_id = "submitted", "vendor-1"
        job.submitted_at = now
        job.deadline_at = now + timedelta(seconds=-1 if outcome == "timeout" else 20)
        await db.commit()
    if outcome == "cancel":
        await jobs.cancel_jobs(ctx, [job_id], actor_id=execution.actor_id)
        assert fake.cancels == 1
    if outcome == "failed":
        fake.polls = [VideoPoll("failed", error_message="内容审核未通过")]
    with pytest.raises(AppError):
        await jobs.drive(ctx, [job_id], execution=execution, emit=emit)
    async with ctx.sessionmaker() as db:
        row = await db.get(AiExternalJob, job_id)
        assert row.state == {"timeout": "expired", "cancel": "cancelled", "failed": "failed"}[outcome]
        assert row.usage_call_id is None
    assert fake.submits == fake.downloads == 0


async def test_stale_execution_cannot_publish_or_send(video_job):
    ctx, fake, job_id, execution = video_job
    async with ctx.sessionmaker() as db:
        (await db.get(Task, execution.task_id)).execution_key = str(uuid4())
        await db.commit()
    with pytest.raises(AppError):
        await jobs.drive(ctx, [job_id], execution=execution, emit=emit)
    assert fake.submits == fake.downloads == 0


async def test_duration_from_download_when_vendor_omits_seconds(video_job, monkeypatch):
    ctx, fake, job_id, execution = video_job
    fake.polls = [VideoPoll("succeeded")]

    def box(name, payload):
        return (len(payload) + 8).to_bytes(4, "big") + name + payload

    async def download(poll):
        header = bytes(12) + (1000).to_bytes(4, "big") + (3500).to_bytes(4, "big") + bytes(80)
        yield box(b"ftyp", b"mp42" + bytes(4)) + box(b"mdat", b"payload") + box(b"moov", box(b"mvhd", header))

    monkeypatch.setattr(fake, "download", download)
    await jobs.drive(ctx, [job_id], execution=execution, emit=emit)
    async with ctx.sessionmaker() as db:
        assert (await db.get(AiUsageCall, job_id)).video_seconds == 4
        assert (await db.get(MediaObject, job_id)).duration_ms == 3500


async def test_resume_abandoned_pending_upload(video_job):
    ctx, fake, job_id, execution = video_job
    async with ctx.sessionmaker() as db:
        job = await db.get(AiExternalJob, job_id)
        now = datetime.now(UTC)
        job.state, job.external_id = "submitted", "vendor-1"
        job.submitted_at, job.deadline_at = now, now + timedelta(minutes=5)
        db.add(MediaObject(id=job_id, location_id=1, source="video_probe", storage_key="abandoned.mp4",
                           owner_user_id=job.actor_id, scope="user", kind="video", mime="video/mp4", status="pending"))
        await db.commit()
    [oid] = await jobs.drive(ctx, [job_id], execution=execution, emit=emit)
    assert oid != job_id and fake.submits == 0 and fake.downloads == 1
    async with ctx.sessionmaker() as db:
        assert (await db.get(AiExternalJob, job_id)).result_object_id == oid
        assert (await db.get(MediaObject, job_id)).deleted_at is not None
        assert (await db.get(MediaObject, oid)).status == "ready"


async def test_batch_parallelism_limits_active_vendor_jobs(video_job, monkeypatch):
    ctx, fake, first, original = video_job
    async with ctx.sessionmaker() as db:
        row = await db.get(AiExternalJob, first)
        ids = [first]
        cfg = await db.get(AIConfig, row.config_id)
        for _ in range(3):
            ids.append(await jobs.create_job(db, ctx, actor_id=row.actor_id, config=cfg,
                                             request=row.request["inputs"]))
        task = await db.get(Task, original.task_id)
        task.status, task.execution_key = "queued", None
        task.input = {**task.input, "external_job_ids": ids}
        execution = from_queued_task(task, str(uuid4()), worker_key=ctx.task_engine.lease.worker_key)
        task.worker_key = execution.worker_key
        task.status, task.execution_key = "running", execution.execution_key
        await db.commit()

    active = set()
    high = 0
    polls = {}

    async def submit(request):
        nonlocal high
        key = str(uuid4())
        active.add(key)
        polls[key] = 0
        high = max(high, len(active))
        return key

    async def poll(key):
        polls[key] += 1
        if polls[key] < 2:
            return VideoPoll("running")
        active.remove(key)
        return VideoPoll("succeeded", duration_seconds=4)

    monkeypatch.setattr(fake, "submit", submit)
    monkeypatch.setattr(fake, "poll", poll)
    assert await jobs.drive(ctx, ids, execution=execution, emit=emit, parallel=2) == ids
    assert high == 2 and len(polls) == len(ids)


async def test_wall_clock_timeout_in_download(video_job, monkeypatch):
    ctx, fake, job_id, execution = video_job
    async with ctx.sessionmaker() as db:
        job = await db.get(AiExternalJob, job_id)
        job.state, job.external_id = "submitted", "vendor-1"
        job.submitted_at = datetime.now(UTC)
        job.deadline_at = datetime.now(UTC) + timedelta(seconds=1.2)
        await db.commit()

    async def slow_download(poll):
        await asyncio.sleep(20)
        yield VIDEO

    monkeypatch.setattr(fake, "download", slow_download)
    with pytest.raises(AppError, match="超时"):
        async with asyncio.timeout(8):
            await jobs.drive(ctx, [job_id], execution=execution, emit=emit)
    async with ctx.sessionmaker() as db:
        assert (await db.get(AiExternalJob, job_id)).state == "expired"
        assert await db.get(MediaObject, job_id) is None


async def test_physical_request_rechecks_config_before_transport(video_job, monkeypatch):
    import httpx

    from app.ai.providers.dispatch_hooks import model_client_event_hooks

    ctx, fake, job_id, execution = video_job
    sent = []

    async def submit(request):
        # 调度通过之后、实际 HTTP 钩子之前更换端点。新端点和旧端点都不能接收此次请求。
        async with ctx.sessionmaker() as db:
            row = await db.get(AiExternalJob, job_id)
            cfg = await db.get(AIConfig, row.config_id)
            cfg.base_url = "https://replacement.example"
            await db.commit()
        async with httpx.AsyncClient(event_hooks=model_client_event_hooks(),
                                     transport=httpx.MockTransport(lambda req: sent.append(req))) as client:
            await client.post("https://original.example/video")
        return "vendor-1"

    monkeypatch.setattr(fake, "submit", submit)
    with pytest.raises(AppError, match="配置已变化"):
        await jobs.drive(ctx, [job_id], execution=execution, emit=emit)
    assert sent == []


async def test_probe_settled_before_done_event_cannot_be_cancelled(video_job, client, system_admin):
    ctx, _, job_id, execution = video_job
    await jobs.drive(ctx, [job_id], execution=execution, emit=emit)
    # 模拟已扣费、执行体还未发出 done 帧，两种受理入口均与结算事务持同一个用户写屏障。
    response = await client.post(f"/api/v1/tasks/{execution.task_id}/cancel", params={"task_key": execution.task_key})
    assert response.status_code == 409 and "视频已完成" in response.text
    response = await system_admin.post(f"/api/v1/admin/tasks/{execution.task_key}/cancel",
                                       json={"confirm": True, "reason": "检查完成收据"})
    assert response.status_code == 409 and "视频已完成" in response.text
    async with ctx.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        assert task.cancel_requested_at is None and task.admin_cancel_operation_id is None
        assert (await db.get(AiExternalJob, job_id)).usage_call_id == job_id


async def test_author_paid_video_uses_frozen_collaborator_grant(app, client, monkeypatch):
    from app.models import Project, ProjectCollaborator, User
    from app.services import project_ai_config

    actor = (await register(client, "video-editor"))["id"]
    ctx = app.state.task_engine.ctx
    fake = FakeVideo()
    monkeypatch.setattr(jobs, "build_video_provider", lambda *args, **kw: fake)
    async with ctx.sessionmaker() as db:
        author = User(username="video-author", password_hash="test")
        db.add(author)
        await db.flush()
        project = Project(user_id=author.id, title="协作视频")
        db.add(project)
        await db.flush()
        editor = ProjectCollaborator(project_id=project.id, user_id=actor, status="accepted", role="editor",
                                     allow_owner_ai=True)
        cfg = AIConfig(user_id=author.id, name="作者视频", provider="openai_compat", model="test-video",
                       model_type="video", is_default=True, api_key_enc=ctx.cipher.encrypt("test"))
        db.add_all([cfg, editor])
        await db.flush()
        binding = await project_ai_config.resolve_project_model(db, actor, project.id, "video", cipher=ctx.cipher)
        args = dict(prompt="test", duration_seconds=4, aspect_ratio="16:9", resolution="720p")
        job_id = await jobs.create_job(db, ctx, actor_id=actor, config=cfg, request=args, project_id=project.id,
                                       project_binding=binding, source_kind="test.video")
        serialized = project_ai_config.serialize_project_model(binding)
        task = Task(user_id=actor, project_id=project.id, kind="test.video",
                    input={"external_job_ids": [job_id], "_project_model": serialized,
                           "_task_actor_token_version": 0, "_collaboration_grant": serialized["grant_identity"]})
        db.add(task)
        await db.flush()
        execution = from_queued_task(task, str(uuid4()), worker_key=ctx.task_engine.lease.worker_key)
        task.worker_key = execution.worker_key
        task.status, task.execution_key = "running", execution.execution_key
        await db.commit()

    assert await jobs.drive(ctx, [job_id], execution=execution, emit=emit) == [job_id]
    async with ctx.sessionmaker() as db:
        call = await db.get(AiUsageCall, job_id)
        assert (call.actor_id, call.payer_id, call.project_id) == (actor, author.id, project.id)
        assert (await db.get(MediaObject, job_id)).owner_user_id == author.id
        member = await db.get(ProjectCollaborator, editor.id)
        member.role = "viewer"
        member.revision += 1
        await db.commit()
    with pytest.raises(AppError):
        await jobs.drive(ctx, [job_id], execution=execution, emit=emit)
    assert fake.submits == 1


async def test_cancel_during_storage_write_cannot_publish(video_job, monkeypatch):
    from app.media.backends import LocalBackend

    ctx, _, job_id, execution = video_job
    put = LocalBackend.put

    async def cancel_after_put(backend, *args, **kwargs):
        size = await put(backend, *args, **kwargs)
        async with ctx.sessionmaker() as db:
            (await db.get(Task, execution.task_id)).cancel_requested_at = datetime.now(UTC)
            await db.commit()
        return size

    monkeypatch.setattr(LocalBackend, "put", cancel_after_put)
    with pytest.raises(asyncio.CancelledError):
        await jobs.drive(ctx, [job_id], execution=execution, emit=emit)
    async with ctx.sessionmaker() as db:
        assert (await db.get(MediaObject, job_id)).status == "deleting"
        assert await db.get(AiUsageCall, job_id) is None


async def test_submission_price_frozen_across_wait(video_job, monkeypatch):
    from app.models.ai_usage import AiModelPrice

    ctx, fake, job_id, execution = video_job
    async with ctx.sessionmaker() as db:
        row = await db.get(AiExternalJob, job_id)
        assert row.request["price"] is None
        key = row.config_instance_key
        db.add(AiModelPrice(config_instance_key=key, provider=row.provider, model=row.model, model_type="video",
                            currency="CNY", video_price_nano=200))
        await db.commit()

    async def poll(external_id):
        async with ctx.sessionmaker() as db:
            (await db.get(AiModelPrice, key)).video_price_nano = 500
            await db.commit()
        return VideoPoll("succeeded", duration_seconds=4)

    monkeypatch.setattr(fake, "poll", poll)
    await jobs.drive(ctx, [job_id], execution=execution, emit=emit)
    async with ctx.sessionmaker() as db:
        call = await db.get(AiUsageCall, job_id)
        assert call.amount_nano == 800
        assert (await db.get(AiExternalJob, job_id)).request["price"]["video_price_nano"] == 200


@pytest.mark.parametrize("admin", [False, True])
async def test_cross_worker_cancel_intent_propagates_to_engine(video_job, admin):
    ctx, fake, job_id, execution = video_job
    async with ctx.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        if admin:
            task.admin_cancel_operation_id = str(uuid4())
            task.admin_cancel_requested_at = datetime.now(UTC)
        else:
            task.cancel_requested_at = datetime.now(UTC)
        await db.commit()
    with pytest.raises(asyncio.CancelledError):
        await jobs.drive(ctx, [job_id], execution=execution, emit=emit)
    assert fake.submits == 0
