"""只在隔离 pytest 进程内记录删除意图、可见性检查和取消关闭的先后。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import aclosing
from pathlib import Path

import pytest
from sqlalchemy import select

MODE = os.environ.get("C7_RACE_MODE", "observe")
assert MODE in {"observe", "lifecycle_first", "cancel_first"}
REPORT = Path(os.environ["C7_RACE_REPORT"])
START = time.monotonic()
EVENTS = []


def mark(name, **fields):
    EVENTS.append({"event": name, "elapsed_s": round(time.monotonic() - START, 6), **fields})


async def state_at(engine, task_id):
    from app.models import Chapter, Project, Task

    async with engine.ctx.sessionmaker() as session:
        row = (await session.execute(
            select(Task.status, Project.deletion_started_at, Project.deleted_at, Chapter.status)
            .join(Project, Project.id == Task.project_id)
            .outerjoin(Chapter, Chapter.id == Task.chapter_id)
            .where(Task.id == task_id)
        )).one_or_none()
    if row is None:
        return {"task_present": False}
    return {"task_present": True, "status": row[0], "deletion_intent": row[1] is not None,
            "deleted": row[2] is not None, "chapter_status": row[3]}


@pytest.fixture(autouse=True)
def observe_case(request, monkeypatch):
    if "test_subscribed_task_deleted_ends_with_error" not in request.node.name:
        return
    from app.services import task_service
    from app.services.task_engine import engine as engine_module
    from app.services.task_engine import peer_stream
    from app.services.task_engine.engine import TaskEngine

    state = {"task_id": None}
    stop_done = asyncio.Event()
    original_subscribe = TaskEngine.subscribe
    original_visible = TaskEngine._visible_task
    original_close = TaskEngine.close_hub
    original_lost = peer_stream._lost
    original_public = peer_stream.subscribe
    original_projection = engine_module.deletion_cancel_event
    original_stop = task_service.stop_project_tasks

    async def subscribe(engine, task_id, *args, **kwargs):
        mark("subscribe_enter", task_id=task_id, follow_local_queue=kwargs.get("follow_local_queue", False),
             heartbeat_s=engine.heartbeat_seconds, poll_s=engine.poll_interval)
        async with aclosing(original_subscribe(engine, task_id, *args, **kwargs)) as stream:
            async for item in stream:
                if item[0] == "snapshot" and (item[1].get("task") or {}).get("status") == "queued":
                    state["task_id"] = task_id
                    mark("queued_snapshot", task_id=task_id)
                yield item

    async def visible(engine, db, task_id, *args, **kwargs):
        if MODE == "cancel_first" and state["task_id"] == task_id and not stop_done.is_set():
            mark("visibility_waits_for_cancel", task_id=task_id)
            await asyncio.wait_for(stop_done.wait(), 3)
            mark("visibility_resumed", task_id=task_id)
        row = await original_visible(engine, db, task_id, *args, **kwargs)
        if row is None and state["task_id"] == task_id:
            mark("visibility_lost", task_id=task_id)
        return row

    def close(engine, task_id, event, data, **kwargs):
        hub = engine.hubs.get(task_id)
        result = original_close(engine, task_id, event, data, **kwargs)
        if state["task_id"] == task_id:
            detail = data.get("detail") if isinstance(data, dict) else None
            if detail not in {"任务不存在", "任务已取消"}:
                detail = "other"
            mark("close_hub", task_id=task_id, frame=event, detail=detail,
                 had_hub=hub is not None, hub_closed=hub.closed if hub is not None else None)
        return result

    def projection(task):
        result = original_projection(task)
        public_task = result[1]["task"]
        fields = ("checkpoint", "skill", "rich_review", "retry_of_id", "chapter_id", "scene_id",
                  "analysis_id", "character_id", "outline_id")
        mark("cancel_projection", task_id=task.id, status=task.status,
             private_references_removed=all(public_task.get(field) is None for field in fields))
        return result

    async def public(engine, binding):
        async with aclosing(original_public(engine, binding)) as stream:
            async for item in stream:
                if item[0] == "error":
                    detail = item[1].get("detail")
                    if detail not in {"任务不存在", "任务已取消"}:
                        detail = "other"
                    task = item[1].get("task")
                    mark("public_error", task_id=binding.task_id, detail=detail, task_null=task is None,
                         task_status=task.get("status") if isinstance(task, dict) else None)
                yield item

    async def lost(engine, binding):
        mark("lost_before", task_id=binding.task_id, **(await state_at(engine, binding.task_id)))
        value = await original_lost(engine, binding)
        detail = value[1].get("detail")
        if detail not in {"任务不存在", "任务已取消"}:
            detail = "other"
        mark("lost_result", task_id=binding.task_id, detail=detail, task_null=value[1].get("task") is None)
        return value

    async def stop(db, engine, project_id):
        task_id = state["task_id"]
        assert task_id is not None
        mark("stop_enter", task_id=task_id, **(await state_at(engine, task_id)))
        if MODE == "lifecycle_first":
            # 强制模拟意图提交与取消完成之间的调度间隔；不替换任何业务判断。
            await asyncio.sleep(1.0)
        mark("real_stop_begin", task_id=task_id)
        try:
            count = await original_stop(db, engine, project_id)
            mark("real_stop_done", task_id=task_id, cancelled_count=count, **(await state_at(engine, task_id)))
            return count
        finally:
            stop_done.set()

    monkeypatch.setattr(TaskEngine, "subscribe", subscribe)
    monkeypatch.setattr(TaskEngine, "_visible_task", visible)
    monkeypatch.setattr(TaskEngine, "close_hub", close)
    monkeypatch.setattr(engine_module, "deletion_cancel_event", projection)
    monkeypatch.setattr(peer_stream, "_lost", lost)
    monkeypatch.setattr(peer_stream, "subscribe", public)
    monkeypatch.setattr(task_service, "stop_project_tasks", stop)


def pytest_runtest_logreport(report):
    if report.when == "call" and "test_subscribed_task_deleted_ends_with_error" in report.nodeid:
        mark("pytest_result", outcome=report.outcome)


def pytest_sessionfinish(session, exitstatus):
    REPORT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with REPORT.open("x", encoding="utf-8") as output:
        json.dump({"mode": MODE, "exitstatus": int(exitstatus), "events": EVENTS}, output,
                  ensure_ascii=False, indent=2)
        output.write("\n")
    REPORT.chmod(0o600)
