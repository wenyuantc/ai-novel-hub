"""自动派生分析只继承原执行任务的真实 actor 与受理令牌轮次；缺证历史任务不补签。"""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import SceneReviewProposal, Task, User
from app.schemas.scene_ai import ChapterTarget
from app.services import analysis_service, rich_document_review
from tests.test_analysis import AUTO_ON, _analysis_tasks, _setup
from tests.test_collaboration_review_claim import _claimed_proposal_world, _persist
from tests.test_rich_document_api import KEY


@pytest.mark.parametrize("settings_overrides", [{**AUTO_ON, "task_max_per_user": 1}])
async def test_chapter_auto_analysis_inherits_original_actor_and_never_renews(app, client, fake_llm, monkeypatch):
    p, _base, (ch,) = await _setup(client)
    original = analysis_service.enqueue_auto
    captured: asyncio.Queue = asyncio.Queue()

    async def held(ctx, project_id, chapter_id, *, execution):
        release = asyncio.Event()
        await captured.put((execution, release))
        await release.wait()
        return None

    monkeypatch.setattr(analysis_service, "enqueue_auto", held)
    fake_llm.push("生成后的正文")
    accepted = await client.post(f"/api/v1/chapters/{ch['id']}/generate")
    assert accepted.status_code == 202, accepted.text
    execution, release = await asyncio.wait_for(captured.get(), 10)
    ctx = app.state.task_engine.ctx
    try:
        assert execution.task_key == accepted.json()["task_key"] and type(execution.actor_token_version) is int
        # 缺少受理令牌的历史身份、伪造的协作授权：都不能以当前数据库值补签作者任务。
        for forged in (replace(execution, actor_token_version=None),
                       replace(execution, grant_identity=(str(uuid4()), 1))):
            with pytest.raises(analysis_service.AnalysisExecutionChanged):
                await original(ctx, p["id"], ch["id"], execution=forged)
        assert await _analysis_tasks(app) == []

        task_id = await original(ctx, p["id"], ch["id"], execution=execution)
        (task,) = await _analysis_tasks(app)
        assert task.id == task_id and task.input["auto"] is True and task.status == "queued"
        # 任务及其配额归原执行者，令牌取自原执行而非当前会话。
        assert (task.user_id, task.input["_task_actor_token_version"]) == (
            execution.actor_id, execution.actor_token_version,
        )

        async with app.state.sessionmaker() as db:
            user = await db.get(User, execution.actor_id)
            user.token_version += 1
            await db.commit()
        # 原令牌轮次被撤销后，旧执行不能用数据库里的新令牌续签派生任务。
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await original(ctx, p["id"], ch["id"], execution=execution)
        assert [t.id for t in await _analysis_tasks(app)] == [task_id]
    finally:
        release.set()


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
@pytest.mark.parametrize("origin", ["trusted", "legacy_without_token", "token_revoked"])
async def test_owner_proposal_auto_analysis_requires_trusted_original_identity(app, client, origin):
    job, plans, logs, _document_url, _source, scene = await _claimed_proposal_world(app, client)
    job.target = ChapterTarget()
    identity = await _persist(app, job, plans, logs, pause=True)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, job.task_id)
        original_token = row.input["_task_actor_token_version"]
        if origin == "legacy_without_token":
            # 升级前的历史任务没有受理令牌。
            row.input = {key: value for key, value in row.input.items() if key != "_task_actor_token_version"}
        elif origin == "token_revoked":
            (await db.get(User, job.user_id)).token_version += 1
        await db.commit()
    if origin == "token_revoked":
        # 作者用新会话采纳；原任务受理时的令牌轮次已被撤销。
        login = await client.post("/api/v1/auth/login", json={"username": "alice", "password": "secret123"})
        assert login.status_code == 200, login.text
    response = await client.post(f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept", json={
        "task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 2}],
        "confirm_format_loss": True,
    })
    assert response.status_code == 200, response.text
    async with app.state.sessionmaker() as db:
        analyses = list(await db.scalars(select(Task).where(Task.kind == "chapter.analyze")))
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
    assert proposal.status == "applied"
    if origin == "trusted":
        assert "analysis_warning" not in response.json()
        assert len(analyses) == 1
        assert (analyses[0].user_id, analyses[0].input["_task_actor_token_version"]) == (job.user_id, original_token)
        return
    # 采纳本身保留，派生分析跳过并明确提示，不以本次会话补签。
    assert response.json()["analysis_warning"] == rich_document_review.REVIEW_AUTO_ORIGIN_UNTRUSTED
    assert analyses == []
