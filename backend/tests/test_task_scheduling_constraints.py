"""C2：两个独立应用争数据库内的名额和队首，同一组用例可在 SQLite / PostgreSQL 16 运行。"""

import asyncio
from collections import defaultdict
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text

from app.models import Task
from tests.helpers import analysis_json, create_ai_config, create_project, register, task_params
from tests.rbac_helpers import set_quota
from tests.schema_helpers import isolated_worker_database


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield {**overrides, "task_max_concurrency": 2, "task_max_per_user": 2}


class DispatchProbe:
    """只控制候选的先后和处理器的退出，领取、数据库事务及后续真实执行均用原实现。"""

    def __init__(self):
        self.started = []
        self.finished = []
        self.exited = defaultdict(asyncio.Event)
        self.closing = False
        self.active = set()
        self.peak = 0
        self.release = defaultdict(asyncio.Event)
        self.entered = defaultdict(asyncio.Event)
        self.attempts = []
        self.selections = []
        self.dispatch = {}

    def observe(self, engine, monkeypatch):
        self.dispatch[engine] = engine._dispatch
        run = engine._run

        async def manual_dispatch():
            pass

        async def observed_run(task_id, *args, **kwargs):
            assert task_id not in self.active
            self.started.append(task_id)
            self.active.add(task_id)
            self.peak = max(self.peak, len(self.active))
            self.entered[task_id].set()
            try:
                if not self.closing:
                    await self.release[task_id].wait()
                return await run(task_id, *args, **kwargs)
            finally:
                self.finished.append(task_id)
                self.active.remove(task_id)
                self.exited[task_id].set()

        monkeypatch.setattr(engine, "_run", observed_run)
        monkeypatch.setattr(engine, "_dispatch", manual_dispatch)

    async def select_and_hold(self, engine, ref, monkeypatch):
        """让真实扫描器在未饱和的数据库快照选中候选，延迟到测试指定时刻才进入领取门。"""
        selected, proceed = asyncio.Event(), asyncio.Event()
        self.selections.append(proceed)
        original = engine._claim_and_run

        async def hold(task_id, key):
            if task_id == ref["id"]:
                assert key == ref["task_key"]
                selected.set()
                await proceed.wait()
                await original(task_id, key)

        with monkeypatch.context() as scoped:
            scoped.setattr(engine, "_claim_and_run", hold)
            await self.dispatch[engine]()
            await asyncio.wait_for(selected.wait(), 8)
        pending = engine._running[ref["id"]]
        self.attempts.append(pending)
        return proceed, pending

    def attempt(self, engine, ref):
        # 直接把库中候选送入领取门，可构造较大 ID 先到和同一队首被另一实例阻挡的交错。
        task_id = ref["id"]
        assert task_id not in engine._running
        coro = engine._claim_and_run(task_id, ref["task_key"])
        pending = asyncio.create_task(coro, name=f"task-{task_id}")
        engine._running[task_id] = pending
        pending.add_done_callback(lambda _: engine._on_done(task_id))
        self.attempts.append(pending)
        return pending

    async def reached(self, task_id):
        await asyncio.wait_for(self.entered[task_id].wait(), 8)

    async def assert_succeeded(self, client, ref):
        await asyncio.wait_for(self.exited[ref["id"]].wait(), 25)
        response = await client.get(f"/api/v1/tasks/{ref['id']}", params=task_params(ref))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "succeeded", response.text

    async def assert_denied(self, engine, ref):
        await self.denied(self.attempt(engine, ref), ref["id"])

    async def denied(self, pending, task_id):
        started = asyncio.create_task(self.entered[task_id].wait())
        try:
            async with asyncio.timeout(8):
                await asyncio.wait([pending, started], return_when=asyncio.FIRST_COMPLETED)
                assert not self.entered[task_id].is_set(), (
                    f"任务 {task_id} 越过约束进入执行体，已同时进入 {sorted(self.active)}"
                )
                await pending
        finally:
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)

    def resume(self, refs):
        for ref in refs:
            self.release[ref["id"]].set()
        for engine, dispatch in self.dispatch.items():
            engine._dispatch = dispatch
            engine.notify()


@pytest.fixture
async def workers(app, second_app, monkeypatch):
    a, b = app.state.task_engine, second_app.state.task_engine
    probe = DispatchProbe()
    for engine in (a, b):
        probe.observe(engine, monkeypatch)
    await b.start()
    assert a.lease.worker_key and b.lease.worker_key and a.lease.worker_key != b.lease.worker_key
    assert a.runtime is not b.runtime and a.ctx.settings is not b.ctx.settings
    assert app.state.engine.pool is not second_app.state.engine.pool
    try:
        yield a, b, probe
    finally:
        probe.closing = True
        for gate in (*probe.selections, *probe.release.values()):
            gate.set()
        if probe.attempts:
            await asyncio.wait_for(asyncio.gather(*probe.attempts, return_exceptions=True), 12)


async def chapter_task(client, project, *, analyze=False):
    response = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章", "content": "原稿"})
    assert response.status_code == 201, response.text
    operation = "analyze" if analyze else "generate"
    response = await client.post(f"/api/v1/chapters/{response.json()['id']}/{operation}", json={})
    assert response.status_code == 202, response.text
    return response.json()


async def states(engine, refs):
    async with engine.ctx.sessionmaker() as db:
        return dict((await db.execute(select(Task.id, Task.status).where(Task.id.in_([r["id"] for r in refs])))).all())


@pytest.mark.parametrize("winner_index", [0, 1], ids=["A-wins", "B-wins"])
@pytest.mark.parametrize("constraint", ["global", "user"])
async def test_two_distinct_candidates_race_for_the_last_slot(
    workers, client, system_admin, fake_llm, monkeypatch, winner_index, constraint,
):
    from sqlalchemy.ext.asyncio import AsyncSession

    a, b, probe = workers
    user = await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    job_count = 6 if constraint == "global" else 3
    refs = [await chapter_task(client, project) for _ in range(job_count)]
    for _ in refs:
        fake_llm.push("完成")
    winner, loser = ((a, b), (b, a))[winner_index]
    first, second = refs[:2]
    release_winner, first_run = await probe.select_and_hold(winner, first, monkeypatch)
    release_loser, contender = await probe.select_and_hold(loser, second, monkeypatch)
    if constraint == "global":
        await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "unlimited"}})
        probe.attempt(winner, refs[2])
        await probe.reached(refs[2]["id"])
    else:
        await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "limited", "value": 1}})

    before_commit, allow_commit = asyncio.Event(), asyncio.Event()
    committed, return_commit, at_gate = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_commit = AsyncSession.commit
    contender_pid = None

    async def hold_winner(db):
        if asyncio.current_task().get_name() != f"task-{first['id']}" or before_commit.is_set():
            return await real_commit(db)
        before_commit.set()
        await allow_commit.wait()
        await real_commit(db)
        committed.set()
        await return_commit.wait()

    def observe_contender(conn, cursor, statement, *_):
        nonlocal contender_pid
        if (asyncio.current_task().get_name() == f"task-{second['id']}"
                and "authorization_state" in statement.lower()
                and statement.lstrip().upper().startswith(("UPDATE", "LOCK TABLE"))):
            if conn.dialect.name == "postgresql":
                contender_pid = conn.connection.driver_connection.get_server_pid()
            at_gate.set()

    peer_db = loser.ctx.sessionmaker.kw["bind"].sync_engine
    monkeypatch.setattr(AsyncSession, "commit", hold_winner)
    event.listen(peer_db, "before_cursor_execute", observe_contender)
    try:
        release_winner.set()
        await asyncio.wait_for(before_commit.wait(), 8)
        release_loser.set()
        await asyncio.wait_for(at_gate.wait(), 8)
        # B 的独立连接已向授权表发出语句；A 仍在门内，尚未提交。
        if contender_pid is not None:
            async with loser.ctx.sessionmaker() as observer, asyncio.timeout(8):
                # 是否阻塞只能从另一条 PostgreSQL 连接观察。
                blocked = text("SELECT cardinality(pg_blocking_pids(:pid))")
                while not await observer.scalar(blocked, {"pid": contender_pid}):  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
        assert not contender.done()
        allow_commit.set()
        await asyncio.wait_for(committed.wait(), 8)
        # A 的数据库已提交，但 A 自己尚未收到回执，此时 B 必须已能区分最后一个名额的归属。
        await probe.denied(contender, second["id"])
        assert await states(loser, [first, second]) == {first["id"]: "running", second["id"]: "queued"}
    finally:
        release_winner.set()
        release_loser.set()
        allow_commit.set()
        return_commit.set()
        event.remove(peer_db, "before_cursor_execute", observe_contender)
    await probe.reached(first["id"])
    if constraint == "user":
        await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "limited", "value": 0}})
        probe.release[first["id"]].set()
        await first_run
        for engine in (a, b):
            await probe.assert_denied(engine, second)
        await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "limited", "value": 1}})

    probe.resume(refs)
    for ref in refs:
        await probe.assert_succeeded(client, ref)
    assert sorted(probe.started) == sorted(ref["id"] for ref in refs)
    assert probe.peak == (2 if constraint == "global" else 1)
    assert len(fake_llm.calls) == job_count


async def test_analysis_fifo_across_workers_including_locked_head(workers, client, fake_llm):
    a, b, probe = workers
    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    refs = [await chapter_task(client, project, analyze=True) for _ in range(3)]
    for _ in refs:
        fake_llm.push([analysis_json()])
    first, second, third = refs
    # PostgreSQL 的队首被另一个事务 FOR UPDATE 锁住时，较大的 ID 仍须看到它。
    # SQLite 用同一顺序运行；其 SELECT 不模仿 PostgreSQL 的行锁。
    async with a.ctx.sessionmaker.begin() as db:
        head = await db.scalar(select(Task).where(Task.id == first["id"]).with_for_update())
        assert head.status == "queued" and head.serial_key == f"analysis:{project['id']}"
        await probe.assert_denied(b, third)
    running = probe.attempt(a, first)
    await probe.reached(first["id"])
    await probe.assert_denied(b, second)
    assert await states(b, refs) == {first["id"]: "running", second["id"]: "queued", third["id"]: "queued"}
    probe.release[first["id"]].set()
    await running
    probe.resume(refs)
    for ref in refs:
        await probe.assert_succeeded(client, ref)
    assert probe.started == probe.finished == [ref["id"] for ref in refs]
    assert probe.peak == 1 and len(fake_llm.calls) == 3


async def test_blocked_owner_does_not_let_editor_pass_but_other_key_runs(
    workers, client, make_client, system_admin, fake_llm,
):
    a, b, probe = workers
    owner = await register(client, "fifo-owner")
    await create_ai_config(client)
    book = await create_project(client)
    editor = make_client()
    await register(editor, "fifo-editor")
    await create_ai_config(editor)
    invitation = await client.post(f"/api/v1/projects/{book['id']}/collaborators", json={
        "username": "fifo-editor", "role": "editor",
    })
    assert invitation.status_code == 201, invitation.text
    invitation = invitation.json()
    accepted = await editor.post(f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
                                 json={"expected_revision": invitation["revision"]})
    assert accepted.status_code == 200, accepted.text

    head = await chapter_task(client, book, analyze=True)
    tail = await chapter_task(editor, book, analyze=True)
    independent = await chapter_task(editor, await create_project(editor), analyze=True)
    for _ in range(3):
        fake_llm.push([analysis_json()])
    await set_quota(system_admin, owner["id"], {"task_concurrency": {"mode": "limited", "value": 0}})
    await probe.assert_denied(a, head)
    await probe.assert_denied(b, tail)
    await probe.dispatch[b]()
    await probe.reached(independent["id"])
    assert await states(a, [head, tail, independent]) == {
        head["id"]: "queued", tail["id"]: "queued", independent["id"]: "running",
    }

    await set_quota(system_admin, owner["id"], {"task_concurrency": {"mode": "limited", "value": 1}})
    probe.resume([head, tail, independent])
    for caller, ref in ((client, head), (editor, tail), (editor, independent)):
        await probe.assert_succeeded(caller, ref)
    assert probe.started[0] == independent["id"]
    assert probe.started.index(head["id"]) < probe.started.index(tail["id"])
    assert len(fake_llm.calls) == 3


async def test_stalled_queue_head_still_blocks_a_peer_with_no_local_backoff(workers, client, fake_llm):
    a, b, probe = workers
    await register(client)
    await create_ai_config(client)
    book = await create_project(client)
    head = await chapter_task(client, book, analyze=True)
    tail = await chapter_task(client, book, analyze=True)
    other = await chapter_task(client, await create_project(client), analyze=True)
    for _ in range(3):
        fake_llm.push([analysis_json()])
    async with a.ctx.sessionmaker() as db:
        first = await db.get(Task, head["id"])
        a._mark_stalled(first.id, first.statistics_key, first.queued_at)
    assert head["id"] not in b._stalled
    await probe.dispatch[a]()
    await probe.reached(other["id"])
    await probe.assert_denied(b, tail)
    assert (await states(b, [head]))[head["id"]] == "queued"
    a.notify()  # 业务重新唤醒后可复查曾退避的队首
    probe.resume([head, tail, other])
    for ref in (head, tail, other):
        await probe.assert_succeeded(client, ref)
    assert probe.started == [other["id"], head["id"], tail["id"]]


async def test_recovered_unconfirmed_row_does_not_occupy_its_key_or_a_global_slot(workers, client, fake_llm):
    a, b, probe = workers
    user = await register(client)
    await create_ai_config(client)
    book = await create_project(client)
    same = await chapter_task(client, book, analyze=True)
    other = await chapter_task(client, await create_project(client), analyze=True)
    async with a.ctx.sessionmaker.begin() as db:
        uncertain = Task(user_id=user["id"], project_id=book["id"], kind="chapter.analyze", status="running",
                         execution_key=str(uuid4()), serial_key=f"analysis:{book['id']}")
        db.add(uncertain)
    await b.recover()
    async with a.ctx.sessionmaker() as db:
        original = await db.get(Task, uncertain.id)
        assert original.status == "running" and original.recovered_at is not None and original.worker_key is None
    fake_llm.push([analysis_json()])
    fake_llm.push([analysis_json()])
    probe.attempt(a, same)
    await probe.reached(same["id"])
    probe.attempt(b, other)
    await probe.reached(other["id"])
    assert probe.peak == 2
    probe.resume([same, other])
    for ref in (same, other):
        await probe.assert_succeeded(client, ref)
    assert len(fake_llm.calls) == 2


async def test_peer_waits_for_capacity_and_resumes_on_its_own_poll(
    workers, client, system_admin, fake_llm, monkeypatch,
):
    a, b, probe = workers
    await b.stop()
    b.poll_interval = 0.2
    await b.start()
    user = await register(client)
    await create_ai_config(client)
    await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "limited", "value": 1}})
    book = await create_project(client)
    first, next_job = [await chapter_task(client, book) for _ in range(2)]
    for _ in range(2):
        fake_llm.push("正文")
    running = probe.attempt(a, first)
    await probe.reached(first["id"])

    # B 曾经拿到过候选，先让它在领取事务内被拒，再放开它的正常后台循环。
    await probe.assert_denied(b, next_job)
    attempts = []
    periods = asyncio.Queue()
    original_claim, original_reap = b._claim_and_run, b._reap_orphans

    async def count_claim(task_id, key):
        attempts.append(task_id)
        await original_claim(task_id, key)

    async def on_period():
        await original_reap()
        periods.put_nowait(None)

    monkeypatch.setattr(b, "_reap_orphans", on_period)
    monkeypatch.setattr(b, "_claim_and_run", count_claim)
    b._dispatch = probe.dispatch[b]
    for _ in range(3):
        b._wake.set()  # 与本地执行体完成相同的唤醒，且不额外清除退避
        await asyncio.wait_for(periods.get(), 8)
    assert len(attempts) <= 3, f"三个周期内重复尝试 {len(attempts)} 次"
    assert (await states(b, [next_job]))[next_job["id"]] == "queued"
    probe.release[first["id"]].set()
    await running
    # A 保持手工调度，不向 B 发送通知。B 只靠自己的定时扫描领取后项。
    await probe.reached(next_job["id"])
    probe.release[next_job["id"]].set()
    await probe.assert_succeeded(client, next_job)
    assert probe.started == [first["id"], next_job["id"]] and probe.peak == 1
