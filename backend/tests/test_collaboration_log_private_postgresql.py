"""真实 PostgreSQL 个人日志、模型归属及任务授权轮次投影。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentChange, AgentConversation, AIConfig, GenerationLog, ProjectCollaborator, Task, User
from app.services import generation_log_service, personal_projection
from app.services.agent import service, transactions
from app.services.project_ai_config import ResolvedProjectModel
from tests import pg_support
from tests.helpers import create_ai_config
from tests.test_agent_recovery_claim_identity import _actor_input, _sign
from tests.test_collaboration_access_postgresql import _setup


@pytest.fixture
async def log_postgres_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url("AINOVEL_COLLAB_LOG_POSTGRES_TEST_URL", "collab_logs", purpose="日志协作测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2,
    ) as apps:
        yield apps


async def test_log_projects_current_actor_and_live_task_grant_after_role_changes(log_postgres_apps):
    owner_app, viewer_app = log_postgres_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=viewer_app), base_url="http://testserver") as viewer,
    ):
        project, user, base = await _setup(owner, viewer)
        chapter = (await owner.post(f"{base}/chapters", json={"title": "日志"})).json()
        config = await create_ai_config(owner)
        async with owner_app.state.sessionmaker() as db:
            member = await db.scalar(select(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
            ))
            task = Task(
                user_id=user["id"], project_id=project["id"], chapter_id=chapter["id"],
                kind="chapter.generate", status="running", input={"_collaboration_grant": {
                    "invitation_id": member.invitation_id, "revision": member.revision,
                }},
            )
            db.add(task)
            await db.flush()
            log = GenerationLog(
                project_id=project["id"], chapter_id=chapter["id"], actor_user_id=user["id"], task_id=task.id,
                ai_config_id=config["id"], kind="generate", status="success",
                params={"task_id": task.id, "ai_config_id": config["id"]},
            )
            db.add(log)
            await db.commit()
            log_id, task_id = log.id, task.id
        url = f"{base}/chapters/{chapter['id']}/generation-logs/{log_id}"
        first = await viewer.get(url)
        assert first.status_code == 200, first.text
        assert first.json()["task_id"] == task_id and first.json()["ai_config_id"] is None
        assert first.json()["params"] == {"task_id": task_id, "ai_config_id": None}
        changed = await owner.patch(f"{base}/collaborators/{user['id']}", json={
            "role": "viewer", "expected_revision": 2,
        })
        assert changed.status_code == 200, changed.text
        after = await viewer.get(url)
        assert after.status_code == 200, after.text
        assert after.json()["task_id"] is None and after.json()["params"]["task_id"] is None
        revoked = await owner.delete(f"{base}/collaborators/{user['id']}", params={"expected_revision": 3})
        assert revoked.status_code == 204, revoked.text
        assert (await viewer.get(url)).status_code == 404


async def test_projection_rechecks_role_and_agent_repeatable_read_membership(log_postgres_apps, monkeypatch):
    owner_app, viewer_app = log_postgres_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=viewer_app), base_url="http://testserver") as viewer,
    ):
        project, user, base = await _setup(owner, viewer)
        chapter = (await owner.post(f"{base}/chapters", json={"title": "日志"})).json()
        async with owner_app.state.sessionmaker() as db:
            member = await db.scalar(select(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
            ))
            task = Task(user_id=user["id"], project_id=project["id"], chapter_id=chapter["id"],
                        kind="chapter.generate", status="running", input={"_collaboration_grant": {
                            "invitation_id": member.invitation_id, "revision": member.revision,
                        }})
            conversation = AgentConversation(user_id=user["id"], project_id=project["id"])
            db.add_all([task, conversation])
            await db.flush()
            log = GenerationLog(project_id=project["id"], chapter_id=chapter["id"], actor_user_id=user["id"],
                                task_id=task.id, kind="generate", status="success", params={"task_id": task.id})
            agent_log = GenerationLog(project_id=project["id"], actor_user_id=user["id"], kind="agent.turn",
                                      status="success", params={"conversation_id": conversation.id},
                                      messages=[{"content": "不应继续返回的私人日志"}])
            db.add_all([log, agent_log])
            await db.commit()
            log_id, conversation_id = log.id, conversation.id

        original_execute = AsyncSession.execute
        changed = False

        async def downgrade_after_task_query(db, statement, *args, **kwargs):
            nonlocal changed
            result = await original_execute(db, statement, *args, **kwargs)
            if not changed and "SELECT tasks.id, tasks.statistics_key" in str(statement):
                changed = True
                response = await owner.patch(f"{base}/collaborators/{user['id']}", json={
                    "expected_revision": 2, "role": "viewer",
                })
                assert response.status_code == 200, response.text
            return result

        monkeypatch.setattr(AsyncSession, "execute", downgrade_after_task_query)
        response = await viewer.get(f"{base}/chapters/{chapter['id']}/generation-logs/{log_id}")
        assert changed and response.status_code == 200, response.text
        assert response.json()["task_id"] is None and response.json()["params"]["task_id"] is None
        monkeypatch.setattr(AsyncSession, "execute", original_execute)

        original_auth = personal_projection.require_personal_project
        calls = 0

        async def revoke_after_projection_auth(db, actor_id, project_id):
            nonlocal calls
            result = await original_auth(db, actor_id, project_id)
            if actor_id == user["id"]:
                calls += 1
                if calls == 2:
                    revoked = await owner.delete(f"{base}/collaborators/{user['id']}",
                                                 params={"expected_revision": 3})
                    assert revoked.status_code == 204, revoked.text
            return result

        monkeypatch.setattr(personal_projection, "require_personal_project", revoke_after_projection_auth)
        response = await viewer.get(f"{base}/agent/conversations/{conversation_id}/generation-logs")
        assert calls >= 2 and response.status_code == 404, response.text
        assert "不应继续返回的私人日志" not in response.text


@pytest.mark.parametrize("changed", ["token", "conversation", "change", "task"])
async def test_agent_decision_uses_fresh_snapshot_after_resume(log_postgres_apps, monkeypatch, changed):
    owner_app, viewer_app = log_postgres_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=viewer_app), base_url="http://testserver") as viewer,
    ):
        project, other, base = await _setup(owner, viewer)
        actor = (await owner.get("/api/v1/auth/me")).json()
        agent_base = f"{base}/agent"
        conversation = (await owner.post(f"{agent_base}/conversations", json={})).json()
        async with owner_app.state.sessionmaker() as db:
            task = Task(user_id=actor["id"], project_id=project["id"], kind="agent.turn", status="paused")
            db.add(task)
            await db.flush()
            change = AgentChange(conversation_id=conversation["id"], project_id=project["id"], turn_task_id=task.id,
                                 turn_task_key=task.statistics_key,
                                 tool_call_id="same", tool_name="test", kind="small", status="proposed",
                                 arguments={"text": "私人提案快照"})
            db.add(change)
            await db.commit()
            task_id, change_id = task.id, change.id
        view = (await owner.get(f"{agent_base}/conversations/{conversation['id']}")).json()["changes"][0]
        identity = {"change_key": view["change_key"], "view_version": view["view_version"]}

        async def update_after_projection_input(_db, _engine, _user_id, _task_id, **_kwargs):
            # 请求的 REPEATABLE READ 事务仍持旧快照；第二连接独立提交新事实。
            async with owner_app.state.sessionmaker() as writer:
                if changed == "token":
                    row = await writer.get(User, actor["id"])
                    row.token_version += 1
                elif changed == "conversation":
                    row = await writer.get(AgentConversation, conversation["id"])
                    row.user_id = other["id"]
                elif changed == "task":
                    row = await writer.get(Task, task_id)
                    row.statistics_key = str(uuid4())
                else:
                    row = await writer.get(AgentChange, change_id)
                    created_at = row.created_at
                    await writer.delete(row)
                    await writer.flush()
                    writer.add(AgentChange(id=change_id, conversation_id=conversation["id"], project_id=project["id"],
                                           turn_task_id=task_id, tool_call_id="same", tool_name="test", kind="small",
                                           status="rejected", created_at=created_at, arguments={"text": "替代提案"}))
                await writer.commit()

        monkeypatch.setattr(service, "maybe_resume", update_after_projection_input)
        response = await owner.post(f"{agent_base}/changes/{change_id}/reject", json=identity)
        assert response.status_code == (401 if changed == "token" else 404), response.text
        assert "私人提案快照" not in response.text and "替代提案" not in response.text


@pytest.mark.parametrize("replacement", ["claim", "task"])
async def test_agent_recovery_cas_on_postgresql(log_postgres_apps, monkeypatch, replacement):
    owner_app, viewer_app = log_postgres_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=viewer_app), base_url="http://testserver") as viewer,
    ):
        project, _, _ = await _setup(owner, viewer)
        actor = (await owner.get("/api/v1/auth/me")).json()
        old_claim, new_claim = str(uuid4()), str(uuid4())
        async with owner_app.state.sessionmaker() as db:
            conversation = AgentConversation(user_id=actor["id"], project_id=project["id"])
            task = Task(user_id=actor["id"], project_id=project["id"], kind="agent.turn", status="paused",
                        input=_actor_input())
            db.add_all([conversation, task])
            await db.flush()
            task_key = task.statistics_key
            change = AgentChange(conversation_id=conversation.id, project_id=project["id"], turn_task_id=task.id,
                                 turn_task_key=task_key,
                                 tool_call_id="cas", tool_name="external", kind="external", status="executing",
                                 executed_at=datetime.now(UTC) - timedelta(minutes=3),
                                 result={transactions.EXTERNAL_CLAIM_KEY: old_claim,
                                         service._EXTERNAL_TASK_KEY: task_key})
            db.add(change)
            old_mac = await _sign(db, task, change)
            await db.commit()
            task_id, change_id = task.id, change.id

        original_execute = AsyncSession.execute
        interleaved = False
        async with owner_app.state.sessionmaker() as db:
            async def swap_after_scan(session, statement, *args, **kwargs):
                nonlocal interleaved
                result = await original_execute(session, statement, *args, **kwargs)
                if session is db and not interleaved and str(statement).lstrip().startswith("SELECT") \
                        and "FROM agent_changes" in str(statement):
                    interleaved = True
                    async with owner_app.state.sessionmaker() as writer:
                        if replacement == "claim":
                            row = await writer.get(AgentChange, change_id)
                            row.result = {transactions.EXTERNAL_CLAIM_KEY: new_claim,
                                          service._EXTERNAL_TASK_KEY: task_key}
                        else:
                            row = await writer.get(Task, task_id)
                            row.statistics_key = str(uuid4())
                        await writer.commit()
                return result

            monkeypatch.setattr(AsyncSession, "execute", swap_after_scan)
            assert await service.recover_external(db, task_id=task_id, engine=owner_app.state.task_engine) == 0
            await db.commit()
            assert interleaved
        async with owner_app.state.sessionmaker() as db:
            current = await db.get(AgentChange, change_id)
            assert current.status == "executing"
            expected = {
                transactions.EXTERNAL_CLAIM_KEY: new_claim if replacement == "claim" else old_claim,
                service._EXTERNAL_TASK_KEY: task_key,
            }
            if replacement == "task":
                expected[service._EXTERNAL_SNAPSHOT_MAC] = old_mac
            assert current.result == expected


async def test_owner_model_source_survives_config_deletion_on_postgresql(log_postgres_apps):
    owner_app, viewer_app = log_postgres_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=viewer_app), base_url="http://testserver") as viewer,
    ):
        project, actor, base = await _setup(owner, viewer)
        author = (await owner.get("/api/v1/auth/me")).json()
        config = await create_ai_config(owner)
        chapter = (await owner.post(f"{base}/chapters", json={"title": "日志"})).json()
        async with owner_app.state.sessionmaker() as db:
            member = await db.scalar(select(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == actor["id"],
            ))
            binding = ResolvedProjectModel(
                project_id=project["id"], actor_id=actor["id"], project_owner_id=author["id"], model_type="text",
                source="owner", config_id=config["id"], config_owner_id=author["id"],
                grant_identity=(member.invitation_id, member.revision), config_fingerprint="a" * 64,
                config_instance_key=(await db.get(AIConfig, config["id"])).instance_key,
            )
            log = await generation_log_service.write_log(
                db, project_id=project["id"], actor_user_id=actor["id"], chapter_id=chapter["id"],
                kind="generate", status="success", ai_config_id=config["id"], provider="PRIVATE_PROVIDER",
                model="PRIVATE_MODEL", params={"nested": {"model": "PRIVATE_MODEL"}}, messages=[],
                model_binding=binding,
            )
            log.prompt_tokens = 31
            await db.commit()
            log_id = log.id
            await db.delete(await db.get(AIConfig, config["id"]))
            await db.commit()
        response = await viewer.get(f"{base}/chapters/{chapter['id']}/generation-logs/{log_id}")
        assert response.status_code == 200, response.text
        assert response.json()["model_source"] == "owner" and response.json()["ai_config_id"] is None
        assert response.json()["provider"] == response.json()["model"] == ""
        assert response.json()["params"]["nested"]["model"] is None and response.json()["prompt_tokens"] == 31
