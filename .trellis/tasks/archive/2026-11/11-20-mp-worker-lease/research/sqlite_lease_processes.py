"""主库持续持写锁时，观察两个实际进程续约、强杀失联和释放锁后的有界接管。"""

import argparse
import asyncio
import json
import multiprocessing
import os
import signal
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import text
from sqlite_contention import StreamWorkload, read_lease, setup

from app.core.database import create_engine, create_sessionmaker
from app.core.security import SecretCipher
from app.models import Task
from app.services import task_statistics_service
from app.services.system_settings_service import SystemSettingsManager
from app.services.task_engine.engine import TaskEngine
from app.services.task_engine.registry import TaskContext, register
from tests.helpers import make_settings


class HeldTransaction(StreamWorkload):
    def __init__(self, role, observer, release):
        super().__init__()
        self.role = role
        self.observer = observer
        self.release = release

    async def run(self, job, ctx):
        if self.role == "victim":
            self.observer.send(("running", os.getpid()))
            await asyncio.Event().wait()
        async with ctx.sessionmaker.begin() as db:
            await db.execute(text("UPDATE users SET username='hold-committed' WHERE id=1"))
            self.observer.send(("locked", os.getpid()))
            if not await asyncio.to_thread(self.release.wait, 180):
                raise TimeoutError("测试协调进程没有释放主库事务")
        self.observer.send(("unlocked", time.monotonic()))
        yield "done", {"probe": True}

    async def on_abort(self, ctx, task, **options):
        self.observer.send(("abort", (os.getpid(), task.title)))


async def worker(directory, role, fast, observer, release, shutdown):
    settings = make_settings(directory, task_max_concurrency=1, task_max_per_user=1)
    db_engine = create_engine(settings.database_url)
    factory = create_sessionmaker(db_engine)
    scheduler = TaskEngine(TaskContext(factory, settings, SecretCipher(settings.encryption_key), None))
    if fast:
        scheduler.heartbeat_interval = 1
        scheduler.lease_seconds = 6
        scheduler.lease_safety_seconds = 1
        scheduler.poll_interval = 1
    manager = SystemSettingsManager(settings, factory, scheduler.notify)
    workload = HeldTransaction(role, observer, release)
    register(workload)
    try:
        await manager.load()
        await scheduler.start()
        observer.send(("registered", (
            scheduler.lease.worker_key, str(scheduler.lease.sqlite_pair.path), scheduler.lease_seconds,
            scheduler.heartbeat_interval, scheduler.poll_interval,
        )))
        async with factory.begin() as db:
            task = Task(user_id=1, kind=workload.kinds[0], title=role, input={"_task_actor_token_version": 0})
            db.add(task)
            await task_statistics_service.record_created(db, task)
        scheduler.notify()
        if role == "victim":
            await asyncio.Event().wait()
        elif not await asyncio.to_thread(shutdown.wait, 180):
            raise TimeoutError("测试协调进程没有结束演练")
    finally:
        await scheduler.stop()
        await manager.close()
        await db_engine.dispose()


def launch(*args):
    asyncio.run(worker(*args))


def receive(pipe, expected, seconds):
    if not pipe.poll(seconds):
        raise AssertionError(f"等待子进程 {expected} 超时")
    name, value = pipe.recv()
    if name != expected:
        raise AssertionError(f"子进程上报 {name}={value}，预期 {expected}")
    return value


def main_facts(directory):
    with closing(sqlite3.connect(f"{(directory / 'test.db').as_uri()}?mode=ro", uri=True, timeout=1)) as conn:
        username = conn.execute("SELECT username FROM users WHERE id=1").fetchone()[0]
        tasks = conn.execute(
            "SELECT t.title, t.status, t.worker_key, t.execution_key, s.status, s.finished_event_at "
            "FROM tasks t JOIN admin_task_statistics s ON s.key=t.statistics_key ORDER BY t.id"
        ).fetchall()
        return username, {row[0]: row[1:] for row in tasks}


def wait_for(observe, seconds):
    expires = time.monotonic() + seconds
    while time.monotonic() < expires:
        result = observe()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError(f"等待 {observe.__name__} 超过 {seconds:g} 秒")


def scenario(directory, fast):
    spawn = multiprocessing.get_context("spawn")
    release, shutdown = spawn.Event(), spawn.Event()
    children = []

    def start(role):
        reader, sender = spawn.Pipe(duplex=False)
        proc = spawn.Process(target=launch, args=(directory, role, fast, sender, release, shutdown))
        proc.start()
        sender.close()
        children.append((proc, reader))
        return proc, reader

    try:
        victim, a = start("victim")
        a_key, source, ttl, tick, poll = receive(a, "registered", 30)
        assert receive(a, "running", 20) == victim.pid
        survivor, b = start("survivor")
        b_key, replica, *_ = receive(b, "registered", 30)
        assert replica == source and b_key != a_key
        assert receive(b, "locked", 20) == survivor.pid
        began = time.monotonic()
        stamps = {key: [read_lease(source, key)[0]] for key in (a_key, b_key)}
        while time.monotonic() - began < ttl + tick:
            for key in stamps:
                stamp, stopped, fenced = read_lease(source, key)
                assert not stopped and not fenced and stamp >= stamps[key][-1]
                if stamp != stamps[key][-1]:
                    stamps[key].append(stamp)
            time.sleep(0.1)
        # 等到的每次变化都已从独立、只读的 SQLite 连接读回；不依赖进程日志或本地时钟推断。
        assert min(map(len, stamps.values())) >= 3, f"持锁期间未持续提交心跳：{stamps}"
        username, tasks = main_facts(directory)
        assert username == "probe"
        for name, owner in (("victim", a_key), ("survivor", b_key)):
            assert tasks[name][:2] == ("running", owner) and tasks[name][2] and tasks[name][4] is None
        alive_hold = time.monotonic() - began

        killed = time.monotonic()
        victim.kill()
        victim.join(5)
        assert victim.exitcode == -signal.SIGKILL
        victim_heartbeat = read_lease(source, a_key)[0]

        def sidecar_fenced():
            stamp, stopped, fenced = read_lease(source, a_key)
            assert not stopped and stamp == victim_heartbeat
            peer_stamp, peer_stopped, peer_fenced = read_lease(source, b_key)
            assert not peer_stopped and not peer_fenced
            return (fenced, peer_stamp) if fenced else None

        # 从真实死亡时刻起计；允许 1 秒的 SQLite 整秒边界和外部采样时间。
        fence_stamp, peer_stamp = wait_for(sidecar_fenced, ttl + poll + 1)
        fence_elapsed = time.monotonic() - killed
        assert peer_stamp > stamps[b_key][-1]
        assert main_facts(directory)[1]["victim"] == tasks["victim"], "主库仍持锁时原归属必须保持"
        released = time.monotonic()
        release.set()
        events = []

        def completed():
            while b.poll():
                events.append(b.recv())
            name, current = main_facts(directory)
            if (name != "hold-committed" or current["survivor"][0] != "succeeded"
                    or current["victim"][0] != "interrupted"):
                return False
            return all(row[2] is None and row[3] == row[0] and row[4] for row in current.values())

        wait_for(completed, poll + 1)
        elapsed = time.monotonic() - released
        shutdown.set()
        survivor.join(15)
        assert survivor.exitcode == 0
        while b.poll():
            try:
                events.append(b.recv())
            except EOFError:
                break
        assert sum(name == "unlocked" for name, _ in events) == 1
        assert [v for name, v in events if name == "abort"] == [(survivor.pid, "victim")]
        _, normal_stop, peer_fence = read_lease(source, b_key)
        assert normal_stop and peer_fence is None
        return {
            "seconds": {"lease": ttl, "heartbeat": tick, "poll": poll},
            "victim_pid": victim.pid, "survivor_pid": survivor.pid,
            "main_lock_healthy_s": round(alive_hold, 2),
            "sidecar_heartbeat_advances_during_healthy_hold": [len(stamps[k]) - 1 for k in (a_key, b_key)],
            "kill_to_sidecar_fence_s": round(fence_elapsed, 2), "sidecar_fenced_at": fence_stamp,
            "main_lock_total_s": round(released - began, 2), "release_to_final_states_s": round(elapsed, 2),
            "victim_status": "interrupted", "survivor_status": "succeeded", "abort_count": 1,
        }
    finally:
        shutdown.set()
        release.set()
        for proc, channel in children:
            if proc.is_alive():
                proc.kill()
            proc.join(5)
            channel.close()
            proc.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true", help="6 秒租约、每秒心跳和调度；默认使用生产参数")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="ainovel-lease-crash-") as name:
        directory = Path(name)
        asyncio.run(setup(directory))
        print(json.dumps(scenario(directory, args.fast), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
