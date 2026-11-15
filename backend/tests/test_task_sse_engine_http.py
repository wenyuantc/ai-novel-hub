"""真实任务引擎帧经受控 ASGI 响应及本机 TCP，不替换来源校验器。"""

import asyncio
import json

from app.services.task_engine.stream_lease import task_sse_response
from tests.helpers import Gate, create_ai_config, create_project, parse_sse, register, wait_until
from tests.test_task_sse_scope import _binding
from tests.test_task_sse_scope_postgresql import _tcp_server


async def test_outline_engine_frames_pass_original_proof_over_tcp(app, client, fake_llm):
    user = await register(client, "engine-stream-owner")
    await create_ai_config(client)
    project = await create_project(client)
    output = json.dumps({
        "volume_title": "启程",
        "outlines": [{"title": "下一章", "summary": "经过验证的正文"}],
    }, ensure_ascii=False)
    first, rest = output[:len(output) // 2], output[len(output) // 2:]
    gate = Gate()
    fake_llm.push([first, gate, rest])
    created = await client.post(f"/api/v1/projects/{project['id']}/outlines/continue", json={"count": 1})
    assert created.status_code == 202, created.text
    task = created.json()
    engine = app.state.task_engine
    await gate.wait_reached()
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None
                     and hub.text == first and hub.snapshot_source() is not None)
    binding = await _binding(app, user["id"], task["id"], task["task_key"])
    entered, release = asyncio.Event(), asyncio.Event()
    release.set()

    async def endpoint():
        events = engine.subscribe(
            binding.task_id, statistics_key=binding.task_key, actor_id=binding.actor.id,
            token_version=binding.actor.token_version, active_grant_required=binding.active_grant_required,
            initial_project_id=binding.project_id, initial_queued_at=binding.queued_at,
            initial_grant=binding.task_grant, guarded=True,
        )
        return task_sse_response(events, sessionmaker=app.state.sessionmaker, binding=binding)

    app.add_api_route("/_test/task-frame", endpoint, methods=["GET"])
    try:
        async with _tcp_server(app, entered, release) as tcp:
            pending = asyncio.create_task(tcp.get("/_test/task-frame"))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                gate.release()
                response = await asyncio.wait_for(pending, 10)
            finally:
                gate.release()
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        assert response.status_code == 200
        events = parse_sse(response.text)
        assert events[0][0] == "snapshot" and events[0][1]["text"] == first
        assert first + "".join(data["text"] for event, data in events if event == "delta") == output
        assert events[-1][0] == "done" and events[-1][1]["task"]["status"] == "succeeded"
        assert events[-1][1]["result"]["drafts"][0]["title"] == "下一章"
    finally:
        gate.release()
