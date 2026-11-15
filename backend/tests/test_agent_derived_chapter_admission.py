"""Agent 派生章节任务不能在缺少原来源及目标实例证明时入队。"""

import pytest
from sqlalchemy import func, select

from app.models import AgentChange, Chapter, ChapterAnalysis, Project, Task
from app.services.agent.tools import ToolCtx, get_tool
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.analysis_service import content_hash
from tests.helpers import create_ai_config, create_project, register, wait_task
from tests.test_agent_runtime import call, change_action, send_task, setup_agent


@pytest.mark.parametrize("kind", ["chapter.generate", "chapter.polish", "chapter.revise"])
@pytest.mark.parametrize("operation", ["preview", "execute"])
async def test_derived_chapter_requires_original_server_proof(app, client, kind, operation):
    user = await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    response = await client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"title": "启程", "content": "原文"},
    )
    assert response.status_code == 201, response.text
    chapter_id = response.json()["id"]
    params = {"chapter_id": chapter_id}
    async with app.state.sessionmaker() as db:
        if kind == "chapter.revise":
            analysis = ChapterAnalysis(project_id=project["id"], chapter_id=chapter_id, chapter_no=1,
                                       content_hash=content_hash("原文"))
            db.add(analysis)
            await db.commit()
            params.update(analysis_id=analysis.id, instruction="请修改")
        project_row = await db.get(Project, project["id"])
        context = ToolCtx(db, project_row, user["id"], app.state.task_engine.ctx)
        tool = get_tool("start_task")
        with pytest.raises(VisibilityPolicyError) as error:
            await getattr(tool, operation)(context, {"kind": kind, "params": params})
        assert error.value.extra == {"code": "agent_derived_chapter_identity_required"}
        await db.rollback()
        assert await db.scalar(select(func.count(Task.id))) == 0
        chapter = await db.get(Chapter, chapter_id)
        assert chapter.content == "原文" and chapter.status == "draft"
        assert chapter.generation_claim_key is None


@pytest.mark.parametrize("origin", [None, {"_agent_task_origin": {"corrupt": True}}])
async def test_confirming_legacy_or_corrupt_derived_proposal_preserves_evidence(
    app, client, fake_llm, origin,
):
    project, base, url = await setup_agent(client)
    response = await client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"title": "启程", "content": "原文"},
    )
    assert response.status_code == 201, response.text
    chapter_id = response.json()["id"]
    fake_llm.push_tools([call("create_character", {"name": "测试提案"})])
    task = await send_task(client, url)
    assert (await wait_task(client, task, statuses=("paused", "failed")))["status"] == "paused"
    async with app.state.sessionmaker() as db:
        change = await db.scalar(select(AgentChange).where(AgentChange.turn_task_id == task["id"]))
        change.tool_name = "start_task"
        change.kind = "task"
        change.arguments = {"kind": "chapter.generate", "params": {"chapter_id": chapter_id}}
        change.preview = {"title": "旧提案"}
        change.result = origin
        change_id = change.id
        await db.commit()

    detail = await client.get(url)
    assert detail.status_code == 200, detail.text
    view = next(row for row in detail.json()["changes"] if row["id"] == change_id)
    assert view["result"] is None
    for _ in range(2):
        response = await client.post(base + f"/changes/{change_id}/confirm", json=change_action(view))
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "agent_derived_chapter_identity_required"
    async with app.state.sessionmaker() as db:
        change = await db.get(AgentChange, change_id)
        assert change.status == "proposed" and change.result == origin
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "chapter.generate")) == 0
        chapter = await db.get(Chapter, chapter_id)
        assert chapter.content == "原文" and chapter.status == "draft"
        assert chapter.generation_claim_key is None
