"""生成任务选用 Skill：创建时快照、重试 / 续跑沿用快照、他人私有 Skill 404、
补充工作流段只在选用时出现、生成记录与 TaskOut。"""

import json

import pytest

from app.ai.prompts import build_chapter_messages, build_polish_messages, build_revise_messages, build_rewrite_messages
from app.ai.providers import AIProviderError
from app.services import batch_service
from tests.helpers import analysis_json, create_ai_config, create_project, register, run_task, wait_task

SKILLS = "/api/v1/skills"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(batch_service, "RETRY_DELAYS", (0.0, 0.0))


async def _setup(client, *, chapters: int = 1, content: str = ""):
    await register(client)
    await create_ai_config(client)
    p = await create_project(client, style="")
    base = f"/api/v1/projects/{p['id']}"
    chs = [
        (await client.post(f"{base}/chapters", json={"title": f"章{i + 1}", "content": content})).json()
        for i in range(chapters)
    ]
    skill = (
        await client.post(
            SKILLS, json={"slug": "flow", "name": "紧凑流程", "description": "d", "body": "第一版工作流：多用短句"}
        )
    ).json()
    return base, chs, skill


def _system(call) -> str:
    return call["messages"][0]["content"]


async def _logs(client, base, ch):
    items = (await client.get(f"{base}/chapters/{ch['id']}/generation-logs")).json()
    return [(await client.get(f"{base}/chapters/{ch['id']}/generation-logs/{i['id']}")).json() for i in items]


def test_skill_guide_only_rendered_when_present():
    kwargs = {"context": "资料", "chapter_no": 1, "title": "一", "summary": "", "target_words": 1000}
    plain = build_chapter_messages(**kwargs)
    assert build_chapter_messages(**kwargs, skill_guide="") == plain
    assert build_chapter_messages(**kwargs, skill_guide="   ") == plain
    guided = build_chapter_messages(**kwargs, skill_guide="步骤一")
    assert guided[1:] == plain[1:]
    assert guided[0]["content"].startswith(plain[0]["content"] + "\n\n【补充工作流")
    assert guided[0]["content"].endswith("步骤一") and "不得改变输出格式" in guided[0]["content"]
    for build, extra in [
        (build_polish_messages, {"content": "正文"}),
        (
            build_revise_messages,
            {"context": "c", "chapter_no": 1, "title": "t", "content": "正文", "suggestions": [], "target_words": 500},
        ),
        (build_rewrite_messages, {"op": "deslop", "selected": "x", "before": "", "after": ""}),
    ]:
        assert build(**extra, skill_guide="") == build(**extra)
        assert "步骤一" in build(**extra, skill_guide="步骤一")[0]["content"]


async def test_generate_snapshots_skill_and_retry_uses_snapshot(client, fake_llm):
    base, (ch,), skill = await _setup(client)
    fake_llm.push(["部分", AIProviderError("模型调用失败：额度不足")])
    resp = await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"skill_id": skill["id"]})
    assert resp.status_code == 202, resp.text
    assert resp.json()["skill"] == {"id": skill["id"], "name": "紧凑流程"}
    task, _ = await run_task(client, resp)
    assert task["status"] == "failed"
    assert "第一版工作流" in _system(fake_llm.calls[-1])
    # 之后修改 Skill：重试仍使用创建时的快照
    assert (await client.patch(f"{SKILLS}/{skill['id']}", json={"body": "第二版工作流"})).status_code == 200
    fake_llm.push("新的正文。")
    resp = await client.post(f"/api/v1/tasks/{task['id']}/retry")
    task, _ = await run_task(client, resp)
    assert task["status"] == "succeeded" and task["skill"]["id"] == skill["id"]
    assert "第一版工作流" in _system(fake_llm.calls[-1]) and "第二版" not in _system(fake_llm.calls[-1])
    logs = await _logs(client, base, ch)
    assert logs[0]["params"]["skill"] == {"id": skill["id"], "name": "紧凑流程"}
    # 删除 Skill 之后仍可重试（沿用该任务创建时的快照，即第二版）
    fake_llm.push(["x", AIProviderError("模型调用失败：再次失败")])
    failed, _ = await run_task(
        client, await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"skill_id": skill["id"]})
    )
    assert (await client.delete(f"{SKILLS}/{skill['id']}")).status_code == 204
    fake_llm.push("又一版正文。")
    task, _ = await run_task(client, await client.post(f"/api/v1/tasks/{failed['id']}/retry"))
    assert task["status"] == "succeeded"
    assert "第二版工作流" in _system(fake_llm.calls[-1])


async def test_generate_without_skill_has_no_guide(client, fake_llm):
    base, (ch,), _ = await _setup(client)
    fake_llm.push("正文。")
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={}))
    assert task["status"] == "succeeded" and task["skill"] is None
    assert "补充工作流" not in _system(fake_llm.calls[-1])
    assert (await _logs(client, base, ch))[0]["params"]["skill"] is None


async def test_global_skill_usable_and_other_private_skill_404(client, make_client, fake_llm):
    base, (ch,), _ = await _setup(client, content="已有的正文内容。")
    deslop = next(s for s in (await client.get(SKILLS)).json() if s["builtin_key"] == "deslop")
    fake_llm.push("润色后。")
    resp = await client.post(f"/api/v1/chapters/{ch['id']}/polish", json={"skill_id": deslop["id"]})
    task, _ = await run_task(client, resp)
    assert task["status"] == "succeeded"
    assert "去 AI 味润色工作流" in _system(fake_llm.calls[-1])

    bob = make_client()
    await register(bob, "bob")
    secret = (await bob.post(SKILLS, json={"slug": "secret", "name": "秘密", "body": "BOB 的秘密流程"})).json()
    calls = len(fake_llm.calls)
    for path, body in [
        (f"/api/v1/chapters/{ch['id']}/generate", {"skill_id": secret["id"]}),
        (f"/api/v1/chapters/{ch['id']}/polish", {"skill_id": secret["id"]}),
        (f"/api/v1/chapters/{ch['id']}/rewrite", {"op": "rewrite", "start": 0, "end": 2, "skill_id": secret["id"]}),
        (
            f"{base}/batch-generate",
            {
                "start_chapter_id": ch["id"],
                "end_chapter_id": ch["id"],
                "include_written": True,
                "skill_id": secret["id"],
            },
        ),
    ]:
        resp = await client.post(path, json=body)
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["detail"] == "Skill 不存在"
    assert len(fake_llm.calls) == calls
    # 章节未被抢占
    assert (await client.get(f"{base}/chapters/{ch['id']}")).json()["status"] == "done"


async def test_rewrite_and_revise_use_skill(client, fake_llm):
    content = "林远推开门。风很大。"
    base, (ch,), skill = await _setup(client, content=content)
    fake_llm.push("改写结果")
    resp = await client.post(
        f"/api/v1/chapters/{ch['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 5, "skill_id": skill["id"]}
    )
    assert resp.status_code == 200
    assert "第一版工作流" in _system(fake_llm.calls[-1])
    logs = await _logs(client, base, ch)
    assert logs[0]["kind"] == "rewrite" and logs[0]["params"]["skill"]["name"] == "紧凑流程"

    fake_llm.push(analysis_json())
    await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/analyze"))
    analysis = (await client.get(f"/api/v1/chapters/{ch['id']}/analyses")).json()[0]
    fake_llm.push("重写后。")
    resp = await client.post(
        f"/api/v1/chapters/{ch['id']}/revise",
        json={"analysis_id": analysis["id"], "suggestion_ids": [1], "skill_id": skill["id"]},
    )
    task, _ = await run_task(client, resp)
    assert task["status"] == "succeeded"
    assert "第一版工作流" in _system(fake_llm.calls[-1])


async def test_batch_snapshot_survives_skill_edit_on_resume(client, fake_llm):
    base, (c1, c2), skill = await _setup(client, chapters=2)
    fake_llm.push("一")
    fake_llm.push("二")
    resp = await client.post(
        f"{base}/batch-generate",
        json={
            "start_chapter_id": c1["id"],
            "end_chapter_id": c2["id"],
            "auto_continue": False,
            "review_every": 1,
            "skill_id": skill["id"],
        },
    )
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["id"]
    assert (await wait_task(client, task_id, statuses=("paused", "failed")))["status"] == "paused"
    await client.patch(f"{SKILLS}/{skill['id']}", json={"body": "第二版工作流"})
    assert (await client.post(f"/api/v1/tasks/{task_id}/resume")).status_code == 202
    assert (await wait_task(client, task_id))["status"] == "succeeded"
    prompts = [_system(c) for c in fake_llm.calls]
    assert len(prompts) == 2 and all("第一版工作流" in p for p in prompts)
    assert (await _logs(client, base, c2))[0]["params"]["skill"]["id"] == skill["id"]


async def test_agent_start_task_skill_reference_checks_visibility(client, make_client, app):
    from app.models import Project
    from app.services.agent.tools import get_tool
    from app.services.agent.tools.base import ToolCtx

    base, (ch,), skill = await _setup(client)
    bob = make_client()
    await register(bob, "bob")
    secret = (await bob.post(SKILLS, json={"slug": "secret", "name": "秘密"})).json()
    tool = get_tool("start_task")
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, int(base.rsplit("/", 1)[1]))
        ctx = ToolCtx(db, project, project.user_id, app.state.task_engine.ctx)
        denied = await tool.preview(
            ctx, {"kind": "chapter.generate", "params": {"chapter_id": ch["id"], "skill_id": secret["id"]}}
        )
        assert denied == {"error": "不存在"}
        allowed = await tool.preview(
            ctx, {"kind": "chapter.generate", "params": {"chapter_id": ch["id"], "skill_id": skill["id"]}}
        )
        assert allowed["skill"]["id"] == skill["id"] and len(allowed["skill"]["hash"]) == 64
        assert any(d["label"] == "Skill" and d["after"] == skill["id"] for d in allowed["diff"])
        json.dumps(allowed)


async def test_validate_references_checks_skill_visibility(client, make_client, app):
    """start_task 的引用校验本身就要拦下他人的私有 Skill（不依赖 handler 里的二次校验）。"""
    from app.core.errors import NotFoundError
    from app.models import Project
    from app.services.agent.tools.base import ToolCtx
    from app.services.agent.tools.references import validate_references

    base, _, skill = await _setup(client)
    bob = make_client()
    await register(bob, "bob")
    secret = (await bob.post(SKILLS, json={"slug": "secret", "name": "秘密"})).json()
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, int(base.rsplit("/", 1)[1]))
        ctx = ToolCtx(db, project, project.user_id, app.state.task_engine.ctx)
        await validate_references(ctx, {"params": {"skill_id": skill["id"]}})
        with pytest.raises(NotFoundError):
            await validate_references(ctx, {"params": {"skill_id": secret["id"]}})
