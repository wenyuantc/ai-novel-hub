"""逐帧内部来源在发布时冻结，不能在消费时用新执行体替换。"""

from uuid import uuid4

from app.services.task_engine.execution_identity import advance_checkpoint, from_queued_task
from app.services.task_engine.hub import TaskHub
from tests.test_task_execution_identity import _queued


def test_guarded_queue_freezes_source_without_changing_legacy_frames():
    task = _queued()
    original = from_queued_task(task, str(uuid4()))
    next_checkpoint = advance_checkpoint(original, {"phase": "next"}, step_cursor=1)
    hub = TaskHub(task.id, statistics_key=task.statistics_key)
    hub.source = original
    _, guarded = hub.subscribe(guarded=True)
    _, legacy = hub.subscribe()
    data = {"text": "原片段"}
    hub.publish("delta", data)
    data["text"] = "被改写"
    hub.source = next_checkpoint
    hub.publish("delta", {"text": "新片段"})
    first, second = guarded.queue.get_nowait(), guarded.queue.get_nowait()
    assert first == ("delta", {"text": "原片段"}, original)
    assert second == ("delta", {"text": "新片段"}, next_checkpoint)
    assert len(legacy.queue.get_nowait()) == 2


def test_snapshot_with_multiple_unproven_publication_sources_fails_closed():
    task = _queued()
    original = from_queued_task(task, str(uuid4()))
    updated = advance_checkpoint(original, {"phase": "next"}, step_cursor=1)
    hub = TaskHub(task.id, statistics_key=task.statistics_key)
    hub.source = original
    hub.append("原")
    assert hub.snapshot_source() == original
    hub.source = updated
    hub.append("新")
    assert hub.snapshot_source() is None
    hub.reset()
    hub.append("重置后")
    assert hub.snapshot_source() == updated
