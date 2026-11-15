"""作者 Agent 派生任务经提案来源证明与中央受控受理入队（E3-A）。"""

import pytest
from sqlalchemy import func, select

from app.models import AgentChange, Chapter, Task, User
from tests.helpers import create_ai_config, wait_task
from tests.test_agent_runtime import call, change_action, send_task, setup_agent


async def _propose(app, client, fake_llm, kind, params_for):
    project, base, url = await setup_agent(client)
    response = await client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"title": "启程", "content": "林远推开门，走进雨夜。"},
    )
    assert response.status_code == 201, response.text
    chapter_id = response.json()["id"]
    fake_llm.push_tools([call("start_task", {"kind": kind, "params": params_for(chapter_id)})])
    turn = await send_task(client, url)
    assert (await wait_task(client, turn, statuses=("paused", "failed")))["status"] == "paused"
    # 后续只验证受理边界；派生任务和续跑轮次不在本测试中执行。
    await app.state.task_engine.stop()
    detail = await client.get(url)
    assert detail.status_code == 200, detail.text
    view = next(row for row in detail.json()["changes"] if row["tool_name"] == "start_task")
    # 来源证明是私有证据：HTTP 投影不返回，数据库保留。
    assert view["status"] == "proposed" and view["result"] is None
    assert "_agent_task_origin" not in detail.text
    return project, base, url, chapter_id, view, turn


@pytest.mark.parametrize(
    "kind,params_for",
    [
        ("character.generate", lambda _cid: {"instruction": "冷静的剑修"}),
        ("outline.continue", lambda _cid: {"count": 2}),
        ("chapter.generate", lambda cid: {"chapter_id": cid}),
        ("chapter.polish", lambda cid: {"chapter_id": cid}),
    ],
)
async def test_confirm_admits_derived_task_with_real_actor_and_server_project(app, client, fake_llm, kind, params_for):
    project, base, _, chapter_id, view, turn = await _propose(app, client, fake_llm, kind, params_for)
    async with app.state.sessionmaker() as db:
        stored = await db.get(AgentChange, view["id"])
        assert set(stored.result) == {"_agent_task_origin"}
        assert stored.result["_agent_task_origin"]["task_key"] == turn["task_key"]
    calls_before = len(fake_llm.calls)
    response = await client.post(base + f"/changes/{view['id']}/confirm", json=change_action(view))
    assert response.status_code == 200, response.text
    change = response.json()
    assert change["status"] == "executed", change
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, change["result"]["task_id"])
        owner = await db.get(User, task.user_id)
        assert task.kind == kind and task.status == "queued"
        assert task.project_id == project["id"] and owner.username
        # 继承轮次的真实 actor 与令牌版本，模型来源由服务端绑定。
        turn_row = await db.get(Task, turn["id"])
        assert task.user_id == turn_row.user_id
        assert task.input["_task_actor_token_version"] == turn_row.input["_agent_actor_token_version"]
        assert task.input["_project_model"]["actor_id"] == task.user_id
        chapter = await db.get(Chapter, chapter_id)
        if kind.startswith("chapter."):
            assert task.chapter_id == chapter_id and task.input["request"]["chapter_id"] == chapter_id
            assert chapter.status == "generating" and chapter.generation_claim_task_key == task.statistics_key
        else:
            assert task.input["request"]["project_id"] == project["id"]
            assert chapter.status == "draft" and chapter.generation_claim_key is None
    # 确认只受理，不调用模型。
    assert len(fake_llm.calls) == calls_before


async def test_tampered_arguments_are_rejected_before_admission(app, client, fake_llm):
    project, base, url, chapter_id, view, _ = await _propose(
        app, client, fake_llm, "chapter.generate", lambda cid: {"chapter_id": cid},
    )
    other = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "次章", "content": "另一章"})
    assert other.status_code == 201, other.text
    async with app.state.sessionmaker() as db:
        stored = await db.get(AgentChange, view["id"])
        stored.arguments = {"kind": "chapter.generate", "params": {"chapter_id": other.json()["id"]}}
        await db.commit()
    detail = (await client.get(url)).json()
    view = next(row for row in detail["changes"] if row["id"] == view["id"])
    response = await client.post(base + f"/changes/{view['id']}/confirm", json=change_action(view))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "agent_derived_chapter_identity_required"
    async with app.state.sessionmaker() as db:
        assert (await db.get(AgentChange, view["id"])).status == "proposed"
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "chapter.generate")) == 0
        for cid in (chapter_id, other.json()["id"]):
            chapter = await db.get(Chapter, cid)
            assert chapter.status == "draft" and chapter.generation_claim_key is None


async def test_replaced_chapter_instance_is_rejected_under_lock(app, client, fake_llm):
    _, base, _, chapter_id, view, _ = await _propose(
        app, client, fake_llm, "chapter.generate", lambda cid: {"chapter_id": cid},
    )
    async with app.state.sessionmaker() as db:
        # 模拟同整数 ID 被另一章节实例复用：签名有效，但锁内实例事实不再匹配。
        chapter = await db.get(Chapter, chapter_id)
        chapter.created_at = chapter.created_at.replace(year=chapter.created_at.year - 1)
        await db.commit()
    response = await client.post(base + f"/changes/{view['id']}/confirm", json=change_action(view))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "agent_derived_chapter_identity_required"
    async with app.state.sessionmaker() as db:
        assert (await db.get(AgentChange, view["id"])).status == "proposed"
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "chapter.generate")) == 0
        chapter = await db.get(Chapter, chapter_id)
        assert chapter.status == "draft" and chapter.generation_claim_key is None


async def test_failed_admission_leaves_no_task_or_claim(app, client, fake_llm):
    project, base, url = await setup_agent(client)
    spare = await create_ai_config(client, name="备用模型")
    chapters = f"/api/v1/projects/{project['id']}/chapters"
    response = await client.post(chapters, json={"title": "启程", "content": "原文"})
    chapter_id = response.json()["id"]
    params = {"chapter_id": chapter_id, "ai_config_id": spare["id"]}
    fake_llm.push_tools([call("start_task", {"kind": "chapter.generate", "params": params})])
    turn = await send_task(client, url)
    assert (await wait_task(client, turn, statuses=("paused", "failed")))["status"] == "paused"
    await app.state.task_engine.stop()
    # 预览不解析 DNS；确认在锁外受理时 DNS 失败，提案记为失败，不建任务、不领取章节占用。
    patched = await client.patch(f"/api/v1/ai-configs/{spare['id']}", json={"base_url": "https://nxdomain.example.test/v1"})
    assert patched.status_code == 200, patched.text
    view = next(row for row in (await client.get(url)).json()["changes"] if row["tool_name"] == "start_task")
    assert view["status"] == "proposed"
    response = await client.post(base + f"/changes/{view['id']}/confirm", json=change_action(view))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "chapter.generate")) == 0
        chapter = await db.get(Chapter, chapter_id)
        assert chapter.status == "draft" and chapter.generation_claim_key is None


async def test_model_reselection_failure_keeps_proposal_pending(app, client, fake_llm, monkeypatch):
    from app.core.errors import ConflictError
    from app.services import project_ai_config

    _, base, _, chapter_id, view, _ = await _propose(
        app, client, fake_llm, "chapter.generate", lambda cid: {"chapter_id": cid},
    )

    async def changed(db, _preflight, **_kwargs):
        # 与真实锁内重选失败一致：先回滚整个根事务再拒绝。
        await db.rollback()
        raise ConflictError("模型授权或配置已变化，请重新发起", extra={"code": "collaboration_busy"})

    monkeypatch.setattr(project_ai_config, "accept_preflight_project_model", changed)
    response = await client.post(base + f"/changes/{view['id']}/confirm", json=change_action(view))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "collaboration_busy"
    async with app.state.sessionmaker() as db:
        change = await db.get(AgentChange, view["id"])
        # 根事务已回滚：不在锁外补写失败，提案保持待确认且来源证明不变。
        assert change.status == "proposed" and set(change.result) == {"_agent_task_origin"}
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "chapter.generate")) == 0
        chapter = await db.get(Chapter, chapter_id)
        assert chapter.status == "draft" and chapter.generation_claim_key is None


async def test_origin_cannot_be_replayed_onto_another_proposal(app, client, fake_llm):
    project, base, url = await setup_agent(client)
    fake_llm.push_tools([
        call("start_task", {"kind": "character.generate", "params": {"instruction": "冷静的剑修"}}, ident="c1"),
        call("start_task", {"kind": "character.generate", "params": {"instruction": "热血的少年"}}, ident="c2"),
    ])
    turn = await send_task(client, url)
    assert (await wait_task(client, turn, statuses=("paused", "failed")))["status"] == "paused"
    await app.state.task_engine.stop()
    views = [row for row in (await client.get(url)).json()["changes"] if row["tool_name"] == "start_task"]
    assert len(views) == 2 and all(row["status"] == "proposed" for row in views)
    first, second = views
    async with app.state.sessionmaker() as db:
        # 把第一项提案的有效来源证明复制到第二项：签名绑定提案身份与参数，不能被重放。
        source = await db.get(AgentChange, first["id"])
        target = await db.get(AgentChange, second["id"])
        target.result = dict(source.result)
        await db.commit()
    second = next(row for row in (await client.get(url)).json()["changes"] if row["id"] == second["id"])
    response = await client.post(base + f"/changes/{second['id']}/confirm", json=change_action(second))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "agent_task_admission_unavailable"
    assert "_agent_task_origin" not in response.text
    async with app.state.sessionmaker() as db:
        assert (await db.get(AgentChange, second["id"])).status == "proposed"
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "character.generate")) == 0


async def test_failure_inside_admission_savepoint_rolls_back_only_admission(app, client, fake_llm, monkeypatch):
    from app.core.errors import ConflictError
    from app.services.task_engine.handlers import chapter as chapter_handler

    _, base, _, chapter_id, view, _ = await _propose(
        app, client, fake_llm, "chapter.generate", lambda cid: {"chapter_id": cid},
    )

    async def refuse(db, task):
        # 章节已在保存点内被抢占为 generating、Task 已插入后才失败。
        raise ConflictError("占用绑定失败")

    monkeypatch.setattr(chapter_handler, "bind_queued_claim", refuse)
    response = await client.post(base + f"/changes/{view['id']}/confirm", json=change_action(view))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert "_agent_task_origin" not in response.text
    async with app.state.sessionmaker() as db:
        change = await db.get(AgentChange, view["id"])
        # 保存点回滚只撤销本次受理；根事务照常提交提案失败结果，不留下任务、占用或证明。
        assert change.status == "failed" and change.result == {"error": "占用绑定失败"}
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "chapter.generate")) == 0
        chapter = await db.get(Chapter, chapter_id)
        assert chapter.status == "draft" and chapter.generation_claim_key is None


async def test_revoked_actor_token_cannot_confirm_derived_task(app, client, fake_llm):
    _, base, _, chapter_id, view, _ = await _propose(
        app, client, fake_llm, "character.generate", lambda _cid: {"instruction": "冷静的剑修"},
    )
    async with app.state.sessionmaker() as db:
        change = await db.get(AgentChange, view["id"])
        turn = await db.get(Task, change.turn_task_id)
        # 原轮次记录的令牌轮次与当前认证不一致（例如提案后会话轮换）：不能用当前身份补签。
        turn.input = {**turn.input, "_agent_actor_token_version": turn.input["_agent_actor_token_version"] + 1}
        await db.commit()
    response = await client.post(base + f"/changes/{view['id']}/confirm", json=change_action(view))
    # 锁内复核原轮次令牌失败关闭：提案记为失败（随后续跑同样因身份不符被拒为 409），不受理任务。
    assert response.status_code == 409, response.text
    assert "_agent_task_origin" not in response.text
    async with app.state.sessionmaker() as db:
        change = await db.get(AgentChange, view["id"])
        assert change.status == "failed" and "_agent_task_origin" not in (change.result or {})
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "character.generate")) == 0
