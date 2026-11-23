"""基线故障注入。配套说明与运行命令见 recovery-reentry.md。"""

from dataclasses import replace

import pytest

from app.models import Chapter, Task
from app.services.task_engine.engine import TaskEngine
from tests.test_chapter_claim_wiring import _queued


class RecoveryStopped(BaseException):
    """在独立清理提交之后、终态事务之前停止第一次恢复。"""


@pytest.mark.parametrize("crash_after_cleanup", [False, True])
async def test_recover_chapter_after_cleanup(app, client, monkeypatch, crash_after_cleanup):
    chapter_id, task_id, key = await _queued(app, client)
    maker = app.state.sessionmaker
    original = app.state.task_engine
    async with maker() as db:
        row = await db.get(Task, task_id)
        original._running_rounds[task_id] = row.queued_at

    async def skip_run(*args, **kwargs):
        pass

    monkeypatch.setattr(original, "_run", skip_run)
    original._stopping = False
    await original._claim_and_run(task_id, key)
    async with maker() as db:
        row = await db.get(Task, task_id)
        chapter = await db.get(Chapter, chapter_id)
        assert row.status == "running" and row.execution_key and "_chapter_claim" in row.input
        assert chapter.status == "generating" and chapter.generation_claim_key

    first = TaskEngine(replace(original.ctx))
    if crash_after_cleanup:
        async def stop_before_terminal(*args, **kwargs):
            assert kwargs["abort_ok"] is True
            raise RecoveryStopped

        monkeypatch.setattr(first, "complete_state", stop_before_terminal)
        with pytest.raises(RecoveryStopped):
            await first.recover()
        async with maker() as db:
            row = await db.get(Task, task_id)
            chapter = await db.get(Chapter, chapter_id)
            assert row.status == "running" and "_chapter_claim" not in row.input
            assert chapter.status == "draft" and chapter.generation_claim_key is None
        last = TaskEngine(replace(original.ctx))
    else:
        last = first
    await last.recover()
    async with maker() as db:
        row = await db.get(Task, task_id)
        chapter = await db.get(Chapter, chapter_id)
        print(f"crash={crash_after_cleanup} status={row.status} "
              f"execution_key_present={row.execution_key is not None} "
              f"cleanup_failure={last.has_cleanup_failure(row.project_id)} "
              f"chapter={chapter.status}")
        assert row.status == "interrupted" and chapter.status == "draft"
        assert (row.execution_key is not None) == crash_after_cleanup
        assert last.has_cleanup_failure(row.project_id) == crash_after_cleanup
