"""用两个独立 TaskEngine 测文件型 SQLite 写竞争。见同目录 sqlite-contention.md。"""

import argparse
import asyncio
import json
import math
import multiprocessing
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import closing
from contextvars import ContextVar
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError

from app.core.database import create_engine, create_sessionmaker
from app.core.security import SecretCipher
from app.models import Base, Task, User
from app.services import authorization_service, task_statistics_service
from app.services.system_settings_service import SystemSettingsManager
from app.services.task_engine.engine import TaskEngine
from app.services.task_engine.registry import TaskContext, register
from tests.helpers import make_settings

PHASE = ContextVar("phase", default="lifecycle")
FRAGMENT = "甲乙丙丁" * 16
CHUNKS = 24


class StreamWorkload:
    """只替换模型与领域处理；真实引擎负责认证、事件发布、2 秒刷库与终态。"""

    kinds = ("outline.continue",)
    execution_identity_aware = True

    def __init__(self):
        self.tasks = []

    async def build_job(self, ctx, task, *, execution):
        self.tasks.append(task.id)
        return execution

    async def run(self, job, ctx):
        for _ in range(CHUNKS):
            await asyncio.sleep(0.1)
            yield "delta", {"text": FRAGMENT}
        yield "done", {"probe": True}

    def progress(self, task, output):
        return min(95, len(output) // len(FRAGMENT))

    async def verify_event(self, db, row, **options):
        pass

    async def verify_finalization(self, db, row, **options):
        pass

    async def on_abort(self, ctx, task, **options):
        pass


def observe(engine, scheduler, timings, errors, matched_writes):
    def track_method(method, label):
        async def measured(*args, **kwargs):
            token = PHASE.set(label)
            try:
                return await method(*args, **kwargs)
            finally:
                PHASE.reset(token)
        return measured

    for method, label in (("_claim_and_run", "claim"), ("_consume", "stream"),
                          ("_flush", "flush"), ("_finalize", "finalize")):
        setattr(scheduler, method, track_method(getattr(scheduler, method), label))

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def begin(conn, cursor, statement, parameters, context, many):
        sql = statement.lower().lstrip()
        if sql.startswith(("insert ", "update ", "delete ")):
            phase = PHASE.get()
            if sql.startswith("update probe_heartbeat set"):
                phase = "heartbeat"
            context.probe = phase, time.perf_counter()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def end(conn, cursor, statement, parameters, context, many):
        if getattr(context, "probe", None):
            phase, start = context.probe
            timings[phase].append((time.perf_counter() - start) * 1000)
            assignment = statement.lower().partition(" where ")[0]
            if cursor.rowcount == 1 and phase == "heartbeat":
                matched_writes["heartbeat"] += 1
            if (cursor.rowcount == 1 and phase == "flush" and assignment.startswith("update tasks set ")
                    and "output_text" in assignment):
                matched_writes["flush"] += 1

    @event.listens_for(engine.sync_engine, "handle_error")
    def on_error(ctx):
        code = getattr(ctx.original_exception, "sqlite_errorname", type(ctx.original_exception).__name__)
        errors[f"{PHASE.get()}/{code}"] += 1


async def baseline_heartbeat(factory, number):
    while True:
        await asyncio.sleep(5)
        async with factory.begin() as db:
            await db.execute(text(
                "UPDATE probe_heartbeat SET heartbeat_at=CURRENT_TIMESTAMP WHERE id=:id"
            ), {"id": number})


def read_lease(path, key):
    """独立只读连接，观察已提交到独立租约文件的事实。"""
    with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)) as conn:
        record = conn.execute(
            "SELECT heartbeat_at, stopped_at, fenced_at FROM worker_leases WHERE worker_key=?", (key,)
        ).fetchone()
        if record is None:
            raise AssertionError(f"缺少独立租约记录 {key}")
        return record


def observe_heartbeat(lease, durations, errors, writes):
    heartbeat = lease.heartbeat

    async def measure():
        key = lease.worker_key
        start = time.perf_counter()
        try:
            confirmed = await heartbeat()
        except Exception as exc:
            errors[f"heartbeat/{type(exc).__name__}"] += 1
            raise
        if confirmed:
            durations.append((time.perf_counter() - start) * 1000)
            stamp, stopped, fenced = read_lease(lease.sqlite_pair.path, key)
            if not stamp or stopped is not None or fenced is not None:
                errors["heartbeat/inconsistent_persistent_state"] += 1
            writes["heartbeat"] += 1
        return confirmed

    lease.heartbeat = measure


async def timeout_control(url, number, barrier):
    """用独立的真实应用连接有意持锁，校准驱动报告超时的位置和耗时。"""
    engine = create_engine(url)
    touch = text("UPDATE users SET id=id WHERE id=1")
    try:
        # 两个调度器均已停止，才允许持长锁，避免校准阻塞另一方的 stopped_at。
        await asyncio.to_thread(barrier.wait, 15)
        if number == 0:
            async with engine.begin() as conn:
                await conn.execute(touch)
                await asyncio.to_thread(barrier.wait, 15)
                await asyncio.to_thread(barrier.wait, 15)
            await asyncio.to_thread(barrier.wait, 15)
            return
        await asyncio.to_thread(barrier.wait, 15)
        start = time.perf_counter()
        try:
            async with engine.begin() as conn:
                await conn.execute(touch)
        except OperationalError as exc:
            code = exc.orig.sqlite_errorname
            if code != "SQLITE_BUSY":
                raise
        else:
            raise AssertionError("持写锁时另一个写者应等到超时")
        waited = round((time.perf_counter() - start) * 1000, 2)
        await asyncio.to_thread(barrier.wait, 15)
        await asyncio.to_thread(barrier.wait, 15)
        async with engine.begin() as conn:
            assert (await conn.execute(touch)).rowcount == 1
        return {"failed_write": code, "wait_ms": waited, "new_transaction_succeeded": True}
    finally:
        await engine.dispose()


async def process_tasks(directory, number, barrier, count, require_lease, control):
    settings = make_settings(directory, task_max_concurrency=2, task_max_per_user=2)
    engine = create_engine(settings.database_url)
    factory = create_sessionmaker(engine)
    scheduler = TaskEngine(TaskContext(factory, settings, SecretCipher(settings.encryption_key), None))
    settings_manager = SystemSettingsManager(settings, factory, scheduler.notify)
    workload = StreamWorkload()
    register(workload)
    timings = defaultdict(list)
    errors = Counter()
    writes = Counter()
    heartbeat_ms = []
    observe(engine, scheduler, timings, errors, writes)
    heartbeat = None
    try:
        await settings_manager.load()
        await scheduler.start()
        real_lease = "task_workers" in Base.metadata.tables
        if require_lease and not real_lease:
            raise AssertionError("需要正式租约实现；当前为基线版本")
        if real_lease:
            key = scheduler.lease.worker_key
            sidecar = scheduler.lease.sqlite_pair.path
            initial_heartbeat, stopped, fenced = read_lease(sidecar, key)
            assert initial_heartbeat and stopped is None and fenced is None
            observe_heartbeat(scheduler.lease, heartbeat_ms, errors, writes)
        else:
            heartbeat = asyncio.create_task(baseline_heartbeat(factory, number))
        # 两个进程都完成启动恢复后才放入任务，基线版本不能安全地恢复另一个进程的任务。
        await asyncio.to_thread(barrier.wait, 25)
        if number == 0:
            async with factory.begin() as db:
                for index in range(count):
                    task = Task(user_id=1, kind=workload.kinds[0], title=f"probe-{index}",
                                input={"_task_actor_token_version": 0})
                    db.add(task)
                    await task_statistics_service.record_created(db, task)
        await asyncio.to_thread(barrier.wait, 25)
        scheduler.notify()
        async with asyncio.timeout(75):
            while True:
                async with factory() as db:
                    statuses = Counter(await db.scalars(select(Task.status)))
                if not (statuses["queued"] or statuses["running"]):
                    break
                await asyncio.sleep(0.2)
        if statuses != {"succeeded": count}:
            raise AssertionError(f"任务结果异常 {statuses}")
        async with factory() as db:
            outputs = (await db.execute(select(Task.output_text, Task.result))).all()
        if not all(value == FRAGMENT * CHUNKS and result == {"probe": True} for value, result in outputs):
            raise AssertionError("流式正文或终态结果不完整")
    finally:
        if heartbeat:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        await scheduler.stop()
        await settings_manager.close()
        await engine.dispose()
    if real_lease:
        heartbeat_at, stopped_at, fenced_at = read_lease(sidecar, key)
        assert heartbeat_at > initial_heartbeat and stopped_at and fenced_at is None
        with closing(sqlite3.connect(directory / "test.db")) as probe:
            records = probe.execute(
                "SELECT worker_key, stopped_at IS NOT NULL, fenced_at IS NULL "
                "FROM task_workers WHERE pid=?", (os.getpid(),)
            ).fetchall()
            if records != [(key, 1, 1)]:
                raise AssertionError(f"主库登记、停止标记与租约文件不符：{records}")
    calibration = await timeout_control(settings.database_url, number, barrier) if control else None
    return os.getpid(), workload.tasks, timings, errors, writes, heartbeat_ms, real_lease, calibration


def run_worker(*args):
    return asyncio.run(process_tasks(*args))


def distribution(values):
    ordered = sorted(values)
    return {"n": len(values), **{
        f"p{q}": round(ordered[math.ceil(len(ordered) * q / 100) - 1], 2) for q in (50, 95, 99, 100)
    }} if ordered else None


async def setup(directory):
    engine = create_engine(make_settings(directory).database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            if "task_workers" not in Base.metadata.tables:
                await conn.exec_driver_sql(
                    "CREATE TABLE probe_heartbeat (id INTEGER PRIMARY KEY, heartbeat_at DATETIME)"
                )
                await conn.exec_driver_sql("INSERT INTO probe_heartbeat(id) VALUES (0),(1)")
            timeout = await conn.scalar(text("PRAGMA busy_timeout"))
            journal = await conn.scalar(text("PRAGMA journal_mode"))
        factory = create_sessionmaker(engine)
        await authorization_service.initialize(factory)
        async with factory.begin() as db:
            db.add(User(id=1, username="probe", password_hash="unused"))
        return timeout, journal
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-lease", action="store_true", help="要求使用真实心跳，供第 7.3 步使用")
    parser.add_argument("--control", action="store_true", help="工作负载结束后，持写锁验证驱动超时与释放后重试")
    parser.add_argument("--tasks", type=int, default=24)
    args = parser.parse_args()
    if args.tasks < 12:
        parser.error("--tasks 至少为 12，以覆盖默认心跳周期")
    spawn = multiprocessing.get_context("spawn")
    with TemporaryDirectory(prefix="ainovel-sqlite-probe-") as name, spawn.Manager() as manager:
        directory = Path(name)
        timeout, journal = asyncio.run(setup(directory))
        timings = defaultdict(list)
        errors = Counter()
        heartbeat_ms = []
        ids = []
        processes = {}
        calibration = None
        barrier = manager.Barrier(2)
        with ProcessPoolExecutor(2, mp_context=spawn) as pool:
            jobs = [pool.submit(run_worker, directory, i, barrier, args.tasks, args.require_lease, args.control)
                    for i in range(2)]
            for job in as_completed(jobs):
                pid, tasks, samples, failures, writes, renewals, real_lease, result = job.result()
                if result:
                    calibration = result
                processes[pid] = {"tasks": len(tasks), "heartbeat": writes["heartbeat"], "flush": writes["flush"]}
                ids.extend(tasks)
                errors.update(failures)
                heartbeat_ms.extend(renewals)
                for phase, values in samples.items():
                    timings[phase].extend(values)
        summary = {phase: distribution(values) for phase, values in sorted(timings.items())}
        print(json.dumps({"python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version, "journal": journal,
                          "busy_timeout_ms": timeout, "real_lease": real_lease, "processes": processes,
                          "main_sql_write_ms": summary, "sidecar_heartbeat_ms": distribution(heartbeat_ms),
                          "db_errors": errors,
                          "timeout_control": calibration}, ensure_ascii=False, indent=2))
        if len(processes) != 2 or not all(min(v.values()) > 0 for v in processes.values()):
            raise AssertionError("两个进程都必须完成任务、刷库、写心跳")
        if errors or sorted(ids) != list(range(1, args.tasks + 1)):
            raise AssertionError("发现数据库异常或任务未被唯一执行")


if __name__ == "__main__":
    main()
