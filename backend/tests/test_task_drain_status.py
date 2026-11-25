"""排空状态直接读取共享的设置、任务、备份和执行者事实。"""

import os
import socket
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select, update

from app.models import BackupRun, Task, TaskExecutionSlot, TaskWorker, User
from tests.helpers import register
from tests.rbac_helpers import assign_roles, create_role
from tests.schema_helpers import isolated_worker_database
from tests.test_system_settings import URL, save_settings

DRAIN_STATUS = f"{URL}/drain-status"


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


async def test_status_requires_settings_read(client, system_admin):
    assert (await client.get(DRAIN_STATUS)).status_code == 401
    user = await register(client, "drain_reader")
    assert (await client.get(DRAIN_STATUS)).status_code == 403
    role = await create_role(system_admin, "排空观察员", ["settings.read"])
    await assign_roles(system_admin, user["id"], [role["id"]])
    assert (await client.get(DRAIN_STATUS)).status_code == 200
    denied = await client.patch(URL, json={"revision": 0, "changes": {"task_admission_closed": True}})
    assert denied.status_code == 403


async def test_two_apps_read_same_drain_facts_without_settings_reload(app, second_app, system_admin):
    # 手工构造确定的持久状态，不让后台领取测试行。
    await app.state.task_engine.stop()
    await second_app.state.system_settings.close()
    await save_settings(system_admin, task_admission_closed=True)
    assert second_app.state.settings.task_admission_closed is False

    async with app.state.sessionmaker.begin() as db:
        user_id = await db.scalar(select(User.id).where(User.username == "settings_admin"))
        worker_key = app.state.task_engine.request_worker.worker_key
        tasks = [
            Task(user_id=user_id, kind="chapter.generate", status=status)
            for status in ("queued", "running", "paused", "succeeded")
        ]
        db.add_all(tasks)
        await db.flush()
        for task in (tasks[1], tasks[3]):
            task.worker_key = worker_key
            task.execution_key = str(uuid4())
            db.add(TaskExecutionSlot(
                execution_key=task.execution_key, worker_key=worker_key, task_id=task.id,
                statistics_key=task.statistics_key, queued_at=task.queued_at, user_id=user_id,
            ))
        db.add(BackupRun(
            execution_key=str(uuid4()), instance_key=str(uuid4()), source="manual", settings_revision=1,
            config_snapshot={}, database_engine=db.get_bind().dialect.name, encrypted=False,
            file_format="sqlite3", status="running", active_slot=1, created_at=datetime.now(UTC),
        ))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=second_app), base_url="http://testserver", cookies=system_admin.cookies,
    ) as peer_admin:
        for admin in (system_admin, peer_admin):
            response = await admin.get(DRAIN_STATUS)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            assert response.json() | {"workers": [], "sampled_at": None} == {
                "enabled": True, "queued": 1, "running": 1, "paused": 1, "finalizing": 1, "active_backups": 1,
                "workers": [], "sampled_at": None,
            }
            assert datetime.fromisoformat(response.json()["sampled_at"]).tzinfo is not None

        # 已进入终态的执行先保留槽。父任务终态到派生入队之间仍有在途工作。
        async with app.state.sessionmaker.begin() as db:
            await db.execute(update(Task).values(status="succeeded"))
            await db.execute(update(BackupRun).values(status="succeeded", active_slot=None))
        before_release = (await peer_admin.get(DRAIN_STATUS)).json()
        assert before_release["queued"] == before_release["running"] == before_release["paused"] == 0
        assert before_release["finalizing"] == 2
        assert before_release["active_backups"] == 0
        async with app.state.sessionmaker.begin() as db:
            await db.execute(delete(TaskExecutionSlot))
        assert (await peer_admin.get(DRAIN_STATUS)).json()["finalizing"] == 0


async def test_worker_view_uses_live_registry_and_does_not_expose_internal_address(app, system_admin):
    engine = app.state.task_engine
    keys = [engine.lease.worker_key, engine.request_worker.worker_key]
    old = datetime.now(UTC) - timedelta(days=1)
    secret_address = "unix:///private/ainovel/internal-peer"
    async with app.state.sessionmaker.begin() as db:
        for row in await db.scalars(select(TaskWorker).where(TaskWorker.worker_key.in_(keys))):
            row.peer_url = secret_address
        db.add(TaskWorker(worker_key=str(uuid4()), hostname="expired", pid=131, heartbeat_at=old))
        db.add(TaskWorker(worker_key=str(uuid4()), hostname="stopped", pid=132, stopped_at=datetime.now(UTC)))
        db.add(TaskWorker(worker_key=str(uuid4()), hostname="fenced", pid=133, fenced_at=datetime.now(UTC)))

    response = await system_admin.get(DRAIN_STATUS)
    assert response.status_code == 200, response.text
    processes = response.json()["workers"]
    assert len(processes) == 1
    assert processes[0]["hostname"] == socket.gethostname()
    assert processes[0]["pid"] == os.getpid()
    assert set(processes[0]) == {"hostname", "pid", "started_at", "heartbeat_at"}
    assert secret_address not in response.text and all(key not in response.text for key in keys)
    assert datetime.fromisoformat(processes[0]["heartbeat_at"]) > old

    # SQLite 主库心跳在启动时记录。实际心跳与失效以配对侧库为准。
    pair = engine.lease.sqlite_pair
    if pair is not None:
        async with app.state.sessionmaker.begin() as db:
            await db.execute(update(TaskWorker).where(TaskWorker.worker_key.in_(keys)).values(heartbeat_at=old))
        still_live = (await system_admin.get(DRAIN_STATUS)).json()["workers"]
        assert len(still_live) == 1 and datetime.fromisoformat(still_live[0]["heartbeat_at"]) > old

        # 保持主库注册，模拟侧库已停止，状态接口只观察而不主动写围栏。
        await engine.stop()
        with closing(sqlite3.connect(pair.path)) as side:
            side.execute("UPDATE worker_leases SET stopped_at=CURRENT_TIMESTAMP WHERE worker_key=?", (keys[1],))
            side.commit()
        assert (await system_admin.get(DRAIN_STATUS)).json()["workers"] == []
        async with app.state.sessionmaker() as db:
            row = await db.get(TaskWorker, keys[1])
            assert row.stopped_at is None and row.fenced_at is None
