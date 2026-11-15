"""排队/终态快照不能用于给没有原执行来源的旧帧补签。"""

import pytest

from app.models import Task
from app.services import ai_dispatch
from app.services.task_engine.stream_evidence import capture_task_view, frame_evidence
from app.services.task_engine.stream_lease import task_sse_response
from tests.test_collaboration_task_privacy import _setup, _tasks
from tests.test_task_sse_scope import _binding


@pytest.mark.parametrize("forgery", ["delta", "snapshot_text", "snapshot_task"])
async def test_queued_task_view_cannot_certify_unproven_data(app, client, make_client, forgery):
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    task_id, task_key = tasks["valid"]
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        row.status, row.output_text = "queued", ""
        await db.commit()
    binding = await _binding(app, editor["id"], task_id, task_key)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        source = capture_task_view(row)
        stamp = await ai_dispatch.read_visibility_stamp(db, project["id"])
    if forgery == "delta":
        event, data = "delta", {"text": "旧执行体敏感数据"}
    elif forgery == "snapshot_text":
        event, data = "snapshot", {"task": None, "text": "旧执行体敏感数据"}
    else:
        event, data = "snapshot", {"task": {"id": task_id, "task_key": task_key,
                                           "title": "旧执行体敏感数据"}, "text": ""}
    evidence = frame_evidence(event, data, source, stamp)

    async def events():
        yield event, data, evidence

    sent = []

    async def send(message):
        sent.append(message)

    await task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding).stream_response(send)
    body = b"".join(item.get("body", b"") for item in sent)
    assert b"permission_denied" in body and "旧执行体敏感数据".encode() not in body
