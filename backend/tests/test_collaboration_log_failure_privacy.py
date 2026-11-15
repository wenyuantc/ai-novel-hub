"""生成日志的失败路径不得把提示词或密钥写入进程日志。"""

import logging
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.models import GenerationLog, User
from app.services import generation_log_service
from app.services.agent import runner, transactions
from tests.helpers import create_project, register, run_task
from tests.test_chapter_versions import _setup


async def test_deleted_actor_log_failure_does_not_log_private_parameters(app, client, make_client, caplog):
    await register(client, "logging-owner")
    collaborator = await register(make_client(), "logging-editor")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        await db.delete(await db.get(User, collaborator["id"]))
        await db.commit()

    prompt = "private-prompt-8f238"
    secret = "private-key-b626a"
    with caplog.at_level(logging.ERROR, logger=generation_log_service.__name__):
        result = await generation_log_service.save_log(
            app.state.sessionmaker,
            project_id=project["id"], actor_user_id=collaborator["id"], chapter_id=None,
            kind="generate", ai_config_id=None, provider="test", model="test",
            params={"api_key": secret}, messages=[{"role": "user", "content": prompt}], status="error",
        )
    assert result is None
    records = [record for record in caplog.records if record.name == generation_log_service.__name__]
    assert records and "IntegrityError" in records[-1].getMessage()
    assert records[-1].exc_info is None
    assert prompt not in caplog.text and secret not in caplog.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(GenerationLog.id).where(GenerationLog.project_id == project["id"])) is None


async def test_direct_log_failure_is_safe_when_caller_logs_exception(app, client, make_client, caplog):
    await register(client, "direct-log-owner")
    collaborator = await register(make_client(), "direct-log-editor")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        await db.delete(await db.get(User, collaborator["id"]))
        await db.commit()

    prompt = "private-direct-prompt-2543"
    secret = "private-direct-key-2418"
    logger = logging.getLogger("tests.log_calling_service")
    with caplog.at_level(logging.ERROR, logger=logger.name):
        async with app.state.sessionmaker() as db:
            try:
                await generation_log_service.write_log(
                    db, project_id=project["id"], actor_user_id=collaborator["id"], chapter_id=None,
                    kind="generate", ai_config_id=None, provider="test", model="test",
                    params={"api_key": secret}, messages=[{"role": "user", "content": prompt}], status="error",
                )
            except Exception as exc:
                logger.exception("任务生成记录失败")
                assert type(exc).__name__ == "GenerationLogWriteError"
                assert "IntegrityError" in str(exc)
                assert exc.__cause__ is None and exc.__context__ is None
            else:
                pytest.fail("缺失 actor 必须使日志写入失败")
    assert prompt not in caplog.text and secret not in caplog.text
    assert "[parameters:" not in caplog.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(GenerationLog.id).where(GenerationLog.project_id == project["id"])) is None


async def test_agent_serialization_failure_remains_a_safe_conflict(monkeypatch):
    prompt = "private-serialization-prompt"

    class SerializationError(Exception):
        sqlstate = "40001"

    class FailingSession:
        def add(self, row):
            pass

        async def flush(self):
            raise DBAPIError("INSERT private", {"prompt": prompt}, SerializationError(prompt))

    async def run_with_write_failure(_job, _ctx):
        await generation_log_service.write_log(
            FailingSession(), project_id=1, actor_user_id=1, chapter_id=None, kind="agent.turn", ai_config_id=None,
            provider="test", model="test", params={"prompt": prompt}, messages=[], status="success",
        )
        yield "done", {}

    with pytest.raises(generation_log_service.GenerationLogWriteError) as caught:
        async for _ in run_with_write_failure(None, None):
            pass
    assert caught.value.serialization_conflict is True
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert prompt not in str(caught.value)
    monkeypatch.setattr(runner, "_run_agent", run_with_write_failure)
    assert [event async for event in runner.run_agent(None, None)] == [
        ("error", {"detail": transactions.CONCURRENT_CHANGE})
    ]


async def test_agent_log_failure_is_safe_after_actor_deleted(app, client, make_client, caplog, monkeypatch):
    await register(client, "agent-log-owner")
    collaborator = await register(make_client(), "agent-log-editor")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        await db.delete(await db.get(User, collaborator["id"]))
        await db.commit()

    prompt = "private-agent-prompt-d114"
    secret = "private-agent-key-5319"
    job = SimpleNamespace(
        project_id=project["id"], user_id=collaborator["id"], task_id=None, conversation_id=1,
        config=SimpleNamespace(id=None, provider="test", model="test"), model_binding=None,
    )
    context = SimpleNamespace(sessionmaker=app.state.sessionmaker)

    # 阶段 E 起 _save_log 先锁原执行体（由 Agent 执行身份专项测试覆盖）；此处只验证其后的日志写入层
    # 在 actor 已删除时安全失败，因此把执行体与资源校验替换为空操作。
    async def skip_execution_checks(_db, _job):
        return None

    monkeypatch.setattr(runner, "_lock_execution", skip_execution_checks)
    monkeypatch.setattr(runner, "verify_job_resources", skip_execution_checks)
    logger = logging.getLogger("tests.agent_log_caller")
    with caplog.at_level(logging.ERROR, logger=logger.name):
        try:
            await runner._save_log(
                job, context, messages=[{"role": "user", "content": prompt}], prompt={"api_key": secret},
                round_no=1, text="output", calls=[], usage={}, started=time.monotonic(), status="success",
            )
        except generation_log_service.GenerationLogWriteError:
            logger.exception("Agent 任务执行失败")
        else:
            pytest.fail("缺失 actor 必须使 Agent 日志写入失败")
    assert "IntegrityError" in caplog.text
    assert prompt not in caplog.text and secret not in caplog.text and "[parameters:" not in caplog.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(GenerationLog.id).where(GenerationLog.project_id == project["id"])) is None


async def test_chapter_generation_log_failure_rolls_back_safely(
    app, client, make_client, fake_llm, monkeypatch, caplog,
):
    project, _, chapter, url = await _setup(client)
    collaborator = await register(make_client(), "chapter-log-editor")
    async with app.state.sessionmaker() as db:
        await db.delete(await db.get(User, collaborator["id"]))
        await db.commit()
    prompt = "private-chapter-prompt-a156"
    secret = "private-chapter-key-8d39"
    original_write = generation_log_service.write_log

    async def write_after_actor_deleted(db, **values):
        # 模拟任务准备后协作者被删除；目前协作入口尚未开放，保留真实章节生成写入链。
        values.update(actor_user_id=collaborator["id"], params={"api_key": secret},
                      messages=[{"role": "user", "content": prompt}])
        return await original_write(db, **values)

    monkeypatch.setattr(generation_log_service, "write_log", write_after_actor_deleted)
    fake_llm.push("生成正文" * 100)
    with caplog.at_level(logging.ERROR):
        task, events = await run_task(
            client, await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={"target_words": 300})
        )
    assert task["status"] == "failed" and events[-1][0] == "error"
    assert prompt not in caplog.text and secret not in caplog.text and "[parameters:" not in caplog.text
    assert prompt not in str(task) and secret not in str(task)
    current = (await client.get(url)).json()
    assert current["content"] == "" and current["status"] == "draft"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(GenerationLog.id).where(GenerationLog.project_id == project["id"])) is None
