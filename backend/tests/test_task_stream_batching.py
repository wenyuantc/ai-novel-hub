"""流合并只处理相邻片段，并在实际发送前复验当前身份。"""

from tests.test_collaboration_task_privacy import _setup, _tasks


async def _stream(app, client, make_client):
    guest = make_client()
    owner, actor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], actor["id"], project["id"], member["invitation_id"], member["revision"])
    task_id, key = tasks["valid"]
    engine = app.state.task_engine
    hub, _ = engine.ensure_hub(task_id, statistics_key=key, status="running")
    stream = engine.subscribe(task_id, statistics_key=key, actor_id=actor["id"], token_version=0,
                              active_grant_required=True)
    assert (await anext(stream))[0] == "snapshot"
    return actor, project, member, engine, hub, stream


async def test_merge_preserves_order_and_never_crosses_progress_or_notification(app, client, make_client):
    actor, project, _, engine, hub, stream = await _stream(app, client, make_client)
    try:
        hub.append("甲")
        hub.append("乙")
        hub.publish("progress", {"status": "running", "stage": "生成", "progress": 10})
        hub.append("丙")
        hub.append("丁")
        engine.notify_collaboration_change(project["id"], actor["id"])
        hub.append("戊")
        hub.append("己")
        assert await anext(stream) == ("delta", {"text": "甲乙"})
        assert await anext(stream) == ("progress", {"status": "running", "stage": "生成", "progress": 10})
        assert await anext(stream) == ("delta", {"text": "丙丁"})
        assert await anext(stream) == ("delta", {"text": "戊己"})
    finally:
        await stream.aclose()


async def test_revoke_after_enqueue_blocks_all_combined_deltas(app, client, make_client):
    actor, project, member, engine, hub, stream = await _stream(app, client, make_client)
    try:
        hub.append("私人甲")
        hub.append("私人乙")
        engine.notify_collaboration_change(project["id"], actor["id"])
        hub.append("私人丙")
        response = await client.delete(f"/api/v1/projects/{project['id']}/collaborators/{actor['id']}", params={
            "expected_revision": member["revision"],
        })
        assert response.status_code == 204
        event, data = await anext(stream)
        assert event == "error" and data["code"] == "permission_denied" and data["task"] is None
        assert "私人" not in str(data)
    finally:
        await stream.aclose()
