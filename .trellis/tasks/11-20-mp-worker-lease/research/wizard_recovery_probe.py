"""向导恢复的基线故障注入，对照结果见 recovery-reentry.md。"""

from dataclasses import replace

import pytest

from app.ai.providers import AIProviderError
from app.models import Task
from app.services import wizard_service
from app.services.task_engine.engine import TaskEngine
from tests.test_collaboration_wizard_entry_e4g3c import _contents, _settled, _start, _world


class ProcessExit(BaseException):
    pass


@pytest.mark.parametrize("crash", [False, True])
async def test_wizard_recovery_gap(app, client, make_client, fake_llm, monkeypatch, crash):
    guest = make_client()
    world = await _world(client, guest)
    before = await _contents(app, world)

    async def unavailable(*args, **kwargs):
        return False

    with monkeypatch.context() as patch:
        patch.setattr(wizard_service, "discard_wizard_pending", unavailable)
        fake_llm.push([AIProviderError("模型调用失败：上游中断")])
        response = await _start(guest, world, "characters")
        assert response.status_code == 202
        stuck = await _settled(app, response.json()["id"])
    assert stuck.status == "running" and "_wizard_pending" in stuck.checkpoint
    await app.state.task_engine.stop()
    b = TaskEngine(replace(app.state.task_engine.ctx))
    if crash:
        release = b._release_wizard_pending

        async def exit_after_commit(task):
            assert await release(task)
            raise ProcessExit

        with monkeypatch.context() as patch:
            patch.setattr(b, "_release_wizard_pending", exit_after_commit)
            with pytest.raises(ProcessExit):
                await b.recover()
        c = TaskEngine(replace(b.ctx))
        await c.recover()
    else:
        await b.recover()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, stuck.id)
        assert task.checkpoint is None or "_wizard_pending" not in task.checkpoint
        if crash:
            assert (task.status, task.execution_key) == ("running", stuck.execution_key)
            assert c.execution_is_uncertain(task)
        else:
            assert (task.status, task.execution_key) == ("interrupted", None)
    assert await _contents(app, world) == before
