"""领取时固定的任务事实不可由后续重读行重新签发。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.errors import ConflictError
from app.models import Task
from app.services.task_engine.execution_identity import from_queued_task, matches_running


def _queued():
    return Task(id=1, statistics_key=str(uuid4()), user_id=2, project_id=3, chapter_id=4,
                kind="wizard.characters", status="queued", queued_at=datetime.now(UTC), execution_key=None,
                step_cursor=0, input={"request": {"chapter_id": 4}, "_wizard_actor_token_version": 2,
                                      "_task_actor_token_version": 2})


@pytest.mark.parametrize("field", [
    "id", "statistics_key", "user_id", "project_id", "chapter_id", "kind", "queued_at", "execution_key",
    "step_cursor", "input", "status",
])
def test_original_execution_rejects_any_replacement(field):
    task = _queued()
    execution = str(uuid4())
    identity = from_queued_task(task, execution)
    task.status, task.execution_key = "running", execution
    assert matches_running(identity, task)
    values = {
        "id": 9, "statistics_key": str(uuid4()), "user_id": 9, "project_id": 9, "chapter_id": 9,
        "kind": "wizard.world", "queued_at": task.queued_at + timedelta(microseconds=1),
        "execution_key": str(uuid4()), "step_cursor": 1, "input": {**task.input, "request": {"chapter_id": True}},
        "status": "queued",
    }
    setattr(task, field, values[field])
    assert not matches_running(identity, task)


def test_execution_snapshot_input_cannot_be_mutated_or_disclosed():
    task = _queued()
    identity = from_queued_task(task, str(uuid4()))
    original = deepcopy(task.input)
    identity.input["request"]["chapter_id"] = 999
    task.input["request"]["chapter_id"] = 123
    assert identity.input == original
    assert "chapter_id" not in repr(identity) and "wizard_actor" not in repr(identity)


@pytest.mark.parametrize("grant", [None, {}, {"invitation_id": str(uuid4()), "revision": True},
                                    {"invitation_id": str(uuid4()), "revision": 1, "extra": 1}])
def test_present_but_invalid_original_grant_fails_closed(grant):
    task = _queued()
    task.input = {**task.input, "_collaboration_grant": grant}
    with pytest.raises(ConflictError):
        from_queued_task(task, str(uuid4()))


@pytest.mark.parametrize("value", [None, True, -1, "2"])
def test_present_but_invalid_actor_token_fails_closed(value):
    task = _queued()
    task.input = {**task.input, "_wizard_actor_token_version": value}
    with pytest.raises(ConflictError):
        from_queued_task(task, str(uuid4()))


def test_personal_projectless_identity_and_missing_historical_token():
    task = _queued()
    task.project_id = task.chapter_id = None
    task.kind = "book.import"
    task.input = {"request": {}}
    identity = from_queued_task(task, str(uuid4()))
    assert identity.project_id is None and identity.actor_token_version is None
