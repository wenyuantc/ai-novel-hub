"""E4i-g / E7：编辑者经真实 HTTP 入口跑 Agent 轮次的 SQLite 端到端账目。

一条轮次同时覆盖：会话压缩摘要（generate_stream）→ 工具轮（search_knowledge 查询向量 + start_task 提案）→ 确认后
派生 chapter.generate（独立模型调用）→ 续跑第二轮。按三种模型来源断言：

- fallback：编辑者本人无文本模型、作者开启模型授权 → 全部文本请求用作者密钥；生成记录/任务内部保留作者配置 ID 与
  `model_source=owner`，编辑者可见的会话日志、任务与提案投影不出现作者配置 ID；查询向量用作者向量（冻结 vector）。
- own_granted：编辑者有本人模型、作者也开启授权 → 本人优先，文本全部用编辑者密钥；向量仍按裁决 12 用作者向量。
- own_ungranted：编辑者本人模型、未授权 → 文本用编辑者密钥，检索仅关键词（零作者向量调用）。

实际请求次数逐类精确断言（摘要 1、工具轮 2、派生 1；向量 = 检索查询 + 派生章节上下文检索，
授权时 2、未授权 0），作者密钥在非回退模式下零次出现；本人配置失效时
不回退作者（零请求、无任务）；轮次中途撤权后不再发出任何请求，已有计数不增加。
"""

import json

import pytest
from sqlalchemy import select, update

from app.models import AgentMessage, AIConfig, GenerationLog, Task
from app.services.task_engine import engine as engine_module
from tests.helpers import Gate, create_ai_config, create_project, register, wait_task
from tests.test_agent_entries_e4id import _detail, _invite, _new_conversation, _send
from tests.test_agent_mcp_skills import change_action
from tests.test_agent_revocation_e4ie import COLLAB_FAILED, TERMINAL, _wait_db
from tests.test_agent_runtime import call
from tests.test_agent_tools_e4ic import _index_owner_kb

OWNER_KEY = "sk-owner-e4ig-000001"
EDITOR_KEY = "sk-editor-e4ig-00001"
STOP = ("paused", "succeeded", "failed", "cancelled", "interrupted")

MODES = {
    # allow_owner_ai, 编辑者有本人文本模型, 期望文本密钥, 期望来源, 是否冻结作者向量
    "fallback": (True, False, OWNER_KEY, "owner", True),
    "own_granted": (True, True, EDITOR_KEY, "actor", True),
    "own_ungranted": (False, True, EDITOR_KEY, "actor", False),
}


def _config_ids(value, found=None):
    """递归收集响应 JSON 中所有 ai_config_id 的取值（含嵌套提案预览、任务结果）。"""
    found = [] if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "ai_config_id":
                found.append(item)
            _config_ids(item, found)
    elif isinstance(value, list):
        for item in value:
            _config_ids(item, found)
    return found


async def _world(app, owner_client, editor_client, mode):
    allow_owner_ai, own, *_ = MODES[mode]
    owner = await register(owner_client, f"e4ig-owner-{mode}")
    owner_cfg = await create_ai_config(owner_client, api_key=OWNER_KEY)
    project = await create_project(owner_client)
    # 关闭自动分析：派生章节成功后不再额外派生分析任务，使请求账目只含本轮可预期的调用。
    patched = await owner_client.patch(f"/api/v1/projects/{project['id']}", json={"auto_analyze": False})
    assert patched.status_code == 200, patched.text
    chapter = await owner_client.post(f"/api/v1/projects/{project['id']}/chapters",
                                      json={"title": "启程", "content": "林远推开门。"})
    assert chapter.status_code == 201, chapter.text
    await _index_owner_kb(app, owner_client, project["id"], owner["id"])
    editor = await register(editor_client, f"e4ig-editor-{mode}")
    editor_cfg = await create_ai_config(editor_client, api_key=EDITOR_KEY) if own else None
    # 编辑者本人的向量配置任何情况下都不参与共享索引检索。
    await create_ai_config(editor_client, name="本人向量", model="editor-embed", model_type="embedding")
    member = await _invite(owner_client, editor_client, project["id"], f"e4ig-editor-{mode}",
                           allow_owner_ai=allow_owner_ai)
    base = f"/api/v1/projects/{project['id']}/agent"
    cid = await _new_conversation(editor_client, base)
    return owner, owner_cfg, project, chapter.json(), editor, editor_cfg, member, base, cid


async def _force_compaction(app, config_id, cid):
    async with app.state.sessionmaker() as db:
        # 预算 = 8k × 0.6 字符：只有第一条超长旧消息需要压缩（恰好一次摘要），工具轮仍在预算内。
        await db.execute(update(AIConfig).where(AIConfig.id == config_id).values(context_window_k=8))
        db.add(AgentMessage(conversation_id=cid, role="user", content="旧消息" + "较早" * 3000))
        db.add(AgentMessage(conversation_id=cid, role="user", content="短的旧消息"))
        await db.commit()


async def _logs_since(app, project_id, after_id):
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(GenerationLog).where(
            GenerationLog.project_id == project_id, GenerationLog.id > after_id,
        ).order_by(GenerationLog.id)))


async def _max_log_id(app):
    async with app.state.sessionmaker() as db:
        return max([row.id for row in await db.scalars(select(GenerationLog))] or [0])


@pytest.mark.parametrize("mode", list(MODES))
async def test_editor_turn_request_ledger_and_config_source(app, client, make_client, fake_llm, mode):
    await check_request_ledger(app, client, make_client(), fake_llm, mode)


async def check_request_ledger(app, client, editor_client, fake_llm, mode):
    """检查端到端账目。"""
    allow_owner_ai, own, text_key, source, vector = MODES[mode]
    owner, owner_cfg, project, chapter, editor, editor_cfg, member, base, cid = await _world(
        app, client, editor_client, mode,
    )
    bound_cfg = editor_cfg if own else owner_cfg
    await _force_compaction(app, bound_cfg["id"], cid)
    calls0, tools0, embeds0 = len(fake_llm.calls), len(fake_llm.tool_calls), len(fake_llm.embed_calls)
    log0 = await _max_log_id(app)
    fake_llm.push("编辑者摘要")
    fake_llm.push_tools([
        call("search_knowledge", {"query": "天剑宗师承"}, "q1"),
        call("start_task", {"kind": "chapter.generate", "params": {"chapter_id": chapter["id"]}}, "s1"),
    ])
    turn = await _send(editor_client, base, cid, "先查资料再生成这一章")
    assert (await wait_task(editor_client, turn, within=10.0, statuses=STOP))["status"] == "paused"
    detail = await _detail(editor_client, base, cid)
    change = next(item for item in detail["changes"] if item["tool_name"] == "start_task")
    confirmed = await editor_client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change))
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "executed", confirmed.text
    derived_id = confirmed.json()["result"]["task_id"]
    async with app.state.sessionmaker() as db:
        derived_key = (await db.get(Task, derived_id)).statistics_key
    assert (await wait_task(editor_client, (derived_id, derived_key), within=10.0))["status"] == "succeeded"
    finished = await wait_task(editor_client, turn, within=10.0)
    assert finished["status"] == "succeeded", (finished.get("error"), finished.get("stage"))
    assert not fake_llm.tool_script and not fake_llm.responses

    # ---- 实际请求次数：摘要 1 + 工具轮 2 + 派生章节 1；查询向量仅在冻结作者向量时 1 次 ----
    new_calls = fake_llm.calls[calls0:]
    new_tools = fake_llm.tool_calls[tools0:]
    plain = [item for item in new_calls if "tools" not in item]
    assert len(new_tools) == 2 and len(plain) == 2 and len(new_calls) == 4
    assert {item["api_key"] for item in new_calls} == {text_key}
    if text_key != OWNER_KEY:
        assert all(item["api_key"] != OWNER_KEY for item in fake_llm.calls[calls0:])
    embeds = fake_llm.embed_calls[embeds0:]
    # 向量请求：工具检索的查询向量 + 派生章节生成的上下文检索（按章节标题）；都经冻结的作者向量（费用=作者）。
    assert [(item["model"], item["texts"]) for item in embeds] == (
        [("owner-embed", ["天剑宗师承"]), ("owner-embed", ["启程"])] if vector else []
    )

    # ---- 内部记录：真实触发者 + 实际配置归属（作者回退时保留作者配置 ID） ----
    owner_id, editor_id = owner["id"], editor["id"]
    config_owner = owner_id if source == "owner" else editor_id
    async with app.state.sessionmaker() as db:
        turn_row = await db.get(Task, turn["id"])
        derived = await db.get(Task, derived_id)
    for row in (turn_row, derived):
        model = row.input["_project_model"]
        assert row.user_id == editor_id
        assert (model["source"], model["config_owner_id"], model["actor_id"], model["config_id"]) == (
            source, config_owner, editor_id, bound_cfg["id"],
        )
        assert row.input["_collaboration_grant"] == {
            "invitation_id": member["invitation_id"], "revision": member["revision"],
        }
    assert turn_row.input["_shared_embedding"]["mode"] == ("vector" if vector else "keyword")
    logs = await _logs_since(app, project["id"], log0)
    kinds = sorted(row.kind for row in logs)
    # 摘要 1 + 工具轮 2 条 agent.turn，派生章节 1 条 generate。
    assert kinds == ["agent.turn", "agent.turn", "agent.turn", "generate"], kinds
    for row in logs:
        # 费用来源与任务绑定一致：日志的 actor 是编辑者，配置与配置所有者是本轮实际使用的那一份。
        assert row.status == "success", (row.kind, row.status, row.error)
        assert (row.actor_user_id, row.model_source, row.ai_config_id, row.model_config_owner_id) == (
            editor_id, source, bound_cfg["id"], config_owner,
        ), row.kind

    # ---- 编辑者可见投影：作者配置 ID 不出现；本人配置 ID 可见 ----
    visible = editor_cfg["id"] if own else None
    shown_logs = await editor_client.get(f"{base}/conversations/{cid}/generation-logs")
    assert shown_logs.status_code == 200, shown_logs.text
    assert len(shown_logs.json()) == 3
    for item in shown_logs.json():
        assert (item["model_source"], item["ai_config_id"]) == (source, visible)
    # 派生章节的 generate 记录经章节级生成记录出口（列表 + 详情）对编辑者可见：同样不得出现作者配置 ID。
    chapter_logs_path = f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}/generation-logs"
    chapter_logs = await editor_client.get(chapter_logs_path)
    assert chapter_logs.status_code == 200, chapter_logs.text
    (generated,) = [item for item in chapter_logs.json() if item["kind"] == "generate"]
    assert (generated["model_source"], generated["ai_config_id"]) == (source, visible)
    generated_detail = await editor_client.get(f"{chapter_logs_path}/{generated['id']}")
    assert generated_detail.status_code == 200, generated_detail.text
    assert generated_detail.json()["ai_config_id"] == visible
    gets = [
        f"/api/v1/tasks/{turn['id']}?task_key={turn['task_key']}",
        f"/api/v1/tasks/{derived_id}?task_key={derived_key}",
        f"/api/v1/tasks/{turn['id']}/output?task_key={turn['task_key']}",
        f"/api/v1/tasks/{derived_id}/output?task_key={derived_key}",
        f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}/versions",
    ]
    responses = [
        shown_logs.json(),
        chapter_logs.json(),
        generated_detail.json(),
        await _detail(editor_client, base, cid),
        (await editor_client.get(f"{base}/conversations")).json(),
        (await editor_client.get("/api/v1/tasks")).json(),
    ]
    for path in gets:
        shown = await editor_client.get(path)
        assert shown.status_code == 200, (path, shown.text)
        responses.append(shown.json())
    for payload in responses:
        text = json.dumps(payload, ensure_ascii=False)
        assert "_project_model" not in text and "_collaboration_grant" not in text and OWNER_KEY not in text
        assert owner_cfg["id"] not in _config_ids(payload)


async def test_invalid_own_config_never_falls_back_to_owner(app, client, make_client, fake_llm):
    """编辑者本人配置存在但不可用（密文损坏）且作者已授权：不回退作者，受理即 400，零请求、不建任务。"""
    editor_client = make_client()
    owner, owner_cfg, project, _chapter, editor, editor_cfg, _member, base, cid = await _world(
        app, client, editor_client, "own_granted",
    )
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(AIConfig.id == editor_cfg["id"]).values(api_key_enc="broken"))
        await db.commit()
        tasks0 = len(list(await db.scalars(select(Task.id).where(Task.user_id == editor["id"]))))
    calls0, embeds0 = len(fake_llm.calls), len(fake_llm.embed_calls)
    response = await editor_client.post(f"{base}/conversations/{cid}/messages", json={"content": "继续"})
    assert response.status_code == 400, response.text
    assert OWNER_KEY not in response.text
    assert len(fake_llm.calls) == calls0 and len(fake_llm.embed_calls) == embeds0
    async with app.state.sessionmaker() as db:
        assert len(list(await db.scalars(select(Task.id).where(Task.user_id == editor["id"])))) == tasks0
    assert owner["id"] != editor["id"] and owner_cfg["id"] != editor_cfg["id"] and project["id"]


@pytest.mark.parametrize("change", ["revoke", "model_off"])
async def test_revocation_mid_turn_sends_nothing_more(app, client, make_client, fake_llm, change):
    """授权回退的轮次在模型流中途被撤权 / 关闭作者模型授权：之后零请求（含查询向量与续跑），计数不增加。"""
    editor_client = make_client()
    owner, _owner_cfg, project, chapter, editor, _editor_cfg, member, base, cid = await _world(
        app, client, editor_client, "fallback",
    )
    gate = Gate()
    fake_llm.push_tools([
        call("search_knowledge", {"query": "天剑宗师承"}, "q1"), gate,
        call("start_task", {"kind": "chapter.generate", "params": {"chapter_id": chapter["id"]}}, "s1"),
    ])
    turn = await _send(editor_client, base, cid, "先查资料再生成这一章")
    await gate.wait_reached(10.0)
    calls, tools, embeds = len(fake_llm.calls), len(fake_llm.tool_calls), len(fake_llm.embed_calls)
    assert tools >= 1 and embeds == 0
    path = f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}"
    if change == "revoke":
        response = await client.delete(path, params={"expected_revision": member["revision"]})
        assert response.status_code == 204, response.text
    else:
        response = await client.patch(path, json={"expected_revision": member["revision"], "allow_owner_ai": False})
        assert response.status_code == 200, response.text
    gate.release()
    row = await _wait_db(app, turn["id"], TERMINAL, within=10.0)
    assert row.status == "failed", (row.status, row.error)
    # 只接受受控的授权失效文案（受限收尾 / 引擎事件围栏 / 发送围栏），不放过无关的内部错误。
    assert row.error in {COLLAB_FAILED, engine_module._POLICY_MESSAGES["task_output_changed"],
                         engine_module._POLICY_MESSAGES["permission_denied"]}, row.error
    assert (len(fake_llm.calls), len(fake_llm.tool_calls), len(fake_llm.embed_calls)) == (calls, tools, embeds)
    async with app.state.sessionmaker() as db:
        derived = list(await db.scalars(select(Task).where(
            Task.user_id == editor["id"], Task.kind == "chapter.generate",
        )))
        assert derived == []
    assert owner["id"] != editor["id"]
