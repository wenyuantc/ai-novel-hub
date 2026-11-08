"""Skill 自定义输入项：占位解析与填充、校验矩阵、各入口快照与重试沿用、预览接口、智能体三条路径、SKILL.md 往返。"""

import json

import pytest
from pydantic import ValidationError

from app.ai import skill_inputs as si
from app.ai.providers import AIProviderError
from app.core.errors import BadRequestError
from app.services import batch_service
from tests.helpers import create_ai_config, create_project, register, run_task, wait_task

SKILLS = "/api/v1/skills"
TONE = {
    "name": "基调", "label": "基调", "kind": "select", "required": False, "default": "轻松",
    "options": ["轻松", "沉重"],
}
HERO = {"name": "hero", "label": "重点人物", "kind": "text", "required": True}
BODY = "基调：{{ 基调 }}；重点人物：{{hero}}；保留 {{ other }}"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(batch_service, "RETRY_DELAYS", (0.0, 0.0))


def _defs():
    return [dict(TONE, help=""), dict(HERO, help="", default="", options=[])]


# ---------------- 纯函数 ----------------


def test_fill_single_pass_and_identity_without_values():
    text = "旧正文 {{x}} 与 {{> comp }} 与 {{ 基调 }}"
    assert si.fill(text, {}) == text
    assert si.placeholders(text) == {"x", "基调"}
    # 只替换声明的标识；值里的 {{ }} / {{> }} 不再解析
    filled = si.fill(text, {"基调": "{{x}}{{> comp }}", "x2": "无关"})
    assert filled == "旧正文 {{x}} 与 {{> comp }} 与 {{x}}{{> comp }}"
    assert si.fill("{{ a }}{{a}}", {"a": "{{ a }}"}) == "{{ a }}{{ a }}"


def test_resolve_matrix_and_defaults():
    defs = _defs()
    assert si.resolve(defs, {"hero": "林远"}) == {"基调": "轻松", "hero": "林远"}
    assert si.resolve(defs, {"hero": "林远", "基调": "沉重"})["基调"] == "沉重"
    cases = [
        ({}, "必填项「重点人物」未填写"),
        ({"hero": "   "}, "必填项「重点人物」未填写"),
        ({"hero": "林远", "基调": "欢快"}, "取值不在选项中"),
        ({"hero": "林远", "nope": "x"}, "未知的输入项：nope"),
        ({"hero": "林" * 201}, "不能超过 200 字"),
    ]
    for raw, message in cases:
        with pytest.raises(BadRequestError) as exc:
            si.resolve(defs, raw)
        assert message in exc.value.detail, raw
    values, missing, errors = si.check(defs, {"基调": "欢快", "x": "1"})
    assert missing == ["hero"] and len(errors) == 2 and values == {"hero": ""}
    # 没有输入项的 Skill：未知标识同样拒绝，空输入放行
    assert si.resolve([], {}) == {}
    with pytest.raises(BadRequestError):
        si.resolve([], {"a": "b"})


def test_warnings_for_undeclared_and_unused():
    warnings = si.warnings(_defs(), "{{ 基调 }} {{ ghost }}")
    assert any("ghost" in w and "没有声明" in w for w in warnings)
    assert any("hero" in w and "没有在正文中引用" in w for w in warnings)
    assert si.warnings([], "没有占位") == []


def test_input_def_and_request_schema_validation():
    from app.schemas.agent import SendMessage
    from app.schemas.batch import BatchGenerateRequest
    from app.schemas.chapter import GenerateChapterRequest, PolishChapterRequest
    from app.schemas.chapter_version import RewriteRequest
    from app.schemas.scene_ai import SceneGenerateRequest, SectionRewriteRequest
    from app.schemas.skill import SkillCreate, SkillInputDef

    bad_defs = [
        dict(TONE, default="欢快"),  # 下拉默认值不在选项中
        dict(TONE, options=["只有一个"], default=""),  # 选项少于 2
        dict(HERO, options=["a", "b"]),  # 非下拉不能有选项
        dict(HERO, name="1abc"),  # 标识以数字开头
        dict(HERO, label="  "),
        dict(HERO, extra="x"),
    ]
    for item in bad_defs:
        with pytest.raises(ValidationError):
            SkillInputDef.model_validate(item)
    with pytest.raises(ValidationError):
        SkillCreate(slug="a", name="a", inputs=[HERO, HERO])
    with pytest.raises(ValidationError):
        SkillCreate(slug="a", name="a", inputs=[dict(HERO, name=f"n{i}") for i in range(21)])

    # skill_inputs 非空但没有 skill_id → 422；总量上限
    for model, extra in [
        (GenerateChapterRequest, {}),
        (PolishChapterRequest, {}),
        (RewriteRequest, {"op": "rewrite", "start": 0, "end": 1}),
        (BatchGenerateRequest, {"start_chapter_id": 1, "end_chapter_id": 1}),
        (SendMessage, {"content": "hi"}),
        (SceneGenerateRequest, {"expected_content_revision": 0, "expected_planning_revision": 0}),
    ]:
        with pytest.raises(ValidationError, match="没有选择 Skill"):
            model.model_validate({**extra, "skill_inputs": {"a": "b"}})
        assert model.model_validate({**extra, "skill_id": 1, "skill_inputs": {"a": "b"}}).skill_inputs == {"a": "b"}
        with pytest.raises(ValidationError):
            model.model_validate({**extra, "skill_id": 1, "skill_inputs": {f"k{i}": "v" for i in range(21)}})
    section = {
        "expected_content_revision": 0, "expected_planning_revision": 0,
        "request_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
        "confirmed": True, "op": "rewrite", "start": 0, "end": 1, "selected_hash": "0" * 64,
    }
    with pytest.raises(ValidationError, match="没有选择 Skill"):
        SectionRewriteRequest.model_validate({**section, "skill_inputs": {"a": "b"}})
    assert SectionRewriteRequest.model_validate({**section, "skill_id": 2, "skill_inputs": {"a": "b"}}).skill_inputs


# ---------------- Skill 库与预览 ----------------


async def _skill(client, *, body=BODY, inputs=None, slug="tone"):
    resp = await client.post(
        SKILLS, json={"slug": slug, "name": "定调", "body": body, "inputs": [TONE, HERO] if inputs is None else inputs}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_skill_crud_with_inputs_warnings_and_preview(client, make_client):
    await register(client)
    skill = await _skill(client)
    assert [d["name"] for d in skill["inputs"]] == ["基调", "hero"]
    assert any("other" in w for w in skill["input_warnings"])
    brief = next(s for s in (await client.get(SKILLS)).json() if s["id"] == skill["id"])
    assert brief["inputs"][0]["options"] == ["轻松", "沉重"]
    # 非法定义 → 422
    resp = await client.post(SKILLS, json={"slug": "bad", "name": "坏", "inputs": [dict(TONE, default="欢快")]})
    assert resp.status_code == 422
    # PATCH 修改定义；不传 inputs 时保持
    resp = await client.patch(f"{SKILLS}/{skill['id']}", json={"inputs": [TONE]})
    assert resp.status_code == 200 and [d["name"] for d in resp.json()["inputs"]] == ["基调"]
    assert any("hero" in w for w in resp.json()["input_warnings"])
    resp = await client.patch(f"{SKILLS}/{skill['id']}", json={"name": "改名"})
    assert [d["name"] for d in resp.json()["inputs"]] == ["基调"]
    await client.patch(f"{SKILLS}/{skill['id']}", json={"inputs": [TONE, HERO]})

    url = f"{SKILLS}/{skill['id']}/preview"
    out = (await client.post(url, json={"inputs": {"hero": "林远"}})).json()
    assert out == {"body": "基调：轻松；重点人物：林远；保留 {{ other }}", "missing_required": [], "errors": []}
    out = (await client.post(url, json={"inputs": {"基调": "欢快", "zz": "1"}})).json()
    assert out["missing_required"] == ["hero"] and len(out["errors"]) == 2
    # 复制带上输入项
    copied = (await client.post(f"{SKILLS}/{skill['id']}/copy")).json()
    assert [d["name"] for d in copied["inputs"]] == ["基调", "hero"]
    # 全局 Skill 可预览；他人私有 Skill 404
    deslop = next(s for s in (await client.get(SKILLS)).json() if s["builtin_key"] == "deslop")
    assert deslop["inputs"] == []
    out = (await client.post(f"{SKILLS}/{deslop['id']}/preview", json={})).json()
    assert "去 AI 味" in out["body"] and out["errors"] == []
    bob = make_client()
    await register(bob, "bob")
    assert (await bob.post(url, json={"inputs": {}})).status_code == 404


async def test_preview_fills_component_content(client):
    await register(client)
    resp = await client.post("/api/v1/prompt-components", json={"slug": "tone-part", "name": "定调片段",
                                                                "content": "组件里的基调：{{ 基调 }}"})
    assert resp.status_code == 201, resp.text
    skill = await _skill(client, body="{{> tone-part }}\n人物：{{ hero }}")
    assert not any("基调" in w for w in skill["input_warnings"])
    resp = await client.post(f"{SKILLS}/{skill['id']}/preview", json={"inputs": {"hero": "林远", "基调": "沉重"}})
    out = resp.json()
    assert out["body"] == "组件里的基调：沉重\n人物：林远"


# ---------------- 生成入口 ----------------


async def _setup(client, *, chapters=1, content=""):
    await register(client)
    await create_ai_config(client)
    p = await create_project(client, style="")
    base = f"/api/v1/projects/{p['id']}"
    chs = [
        (await client.post(f"{base}/chapters", json={"title": f"章{i + 1}", "content": content})).json()
        for i in range(chapters)
    ]
    return base, chs, await _skill(client)


def _system(call) -> str:
    return call["messages"][0]["content"]


async def _logs(client, base, ch):
    items = (await client.get(f"{base}/chapters/{ch['id']}/generation-logs")).json()
    return [(await client.get(f"{base}/chapters/{ch['id']}/generation-logs/{i['id']}")).json() for i in items]


async def test_generate_fills_inputs_rejects_invalid_and_retry_keeps_snapshot(client, fake_llm):
    base, (ch,), skill = await _setup(client)
    url = f"/api/v1/chapters/{ch['id']}/generate"
    calls = len(fake_llm.calls)
    for body, status in [
        ({"skill_id": skill["id"]}, 400),  # 缺必填
        ({"skill_id": skill["id"], "skill_inputs": {"hero": "林远", "基调": "欢快"}}, 400),
        ({"skill_id": skill["id"], "skill_inputs": {"hero": "林远", "nope": "x"}}, 400),
        ({"skill_id": skill["id"], "skill_inputs": {"hero": "林" * 201}}, 400),
        ({"skill_inputs": {"hero": "林远"}}, 422),
    ]:
        resp = await client.post(url, json=body)
        assert resp.status_code == status, (body, resp.text)
    assert len(fake_llm.calls) == calls
    assert (await client.get(f"{base}/chapters/{ch['id']}")).json()["status"] != "generating"

    fake_llm.push(["部分", AIProviderError("模型调用失败：额度不足")])
    resp = await client.post(url, json={"skill_id": skill["id"], "skill_inputs": {"hero": "林远"}})
    assert resp.status_code == 202, resp.text
    task, _ = await run_task(client, resp)
    assert task["status"] == "failed"
    system = _system(fake_llm.calls[-1])
    assert "基调：轻松；重点人物：林远；保留 {{ other }}" in system
    # 修改输入项与正文后重试：仍用原值原正文
    await client.patch(f"{SKILLS}/{skill['id']}", json={"body": "第二版 {{ hero }}", "inputs": [HERO]})
    fake_llm.push("新的正文。")
    task, _ = await run_task(client, await client.post(f"/api/v1/tasks/{task['id']}/retry"))
    assert task["status"] == "succeeded"
    assert "基调：轻松；重点人物：林远" in _system(fake_llm.calls[-1]) and "第二版" not in _system(fake_llm.calls[-1])
    logs = await _logs(client, base, ch)
    assert logs[0]["params"]["skill"] == {"id": skill["id"], "name": "定调", "inputs": {"基调": "轻松", "hero": "林远"}}


async def test_skill_without_inputs_keeps_literal_braces(client, fake_llm):
    base, (ch,), _ = await _setup(client)
    plain = await _skill(client, slug="plain", body="旧正文 {{x}} 原样", inputs=[])
    fake_llm.push("正文。")
    resp = await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"skill_id": plain["id"]})
    task, _ = await run_task(client, resp)
    assert task["status"] == "succeeded" and "旧正文 {{x}} 原样" in _system(fake_llm.calls[-1])
    assert (await _logs(client, base, ch))[0]["params"]["skill"] == {"id": plain["id"], "name": "定调"}
    # 没有输入项却提交了输入 → 未知标识 400
    resp = await client.post(
        f"/api/v1/chapters/{ch['id']}/generate", json={"skill_id": plain["id"], "skill_inputs": {"x": "1"}}
    )
    assert resp.status_code == 400 and "未知的输入项" in resp.json()["detail"]


async def test_polish_and_rewrite_fill_inputs(client, fake_llm):
    base, (ch,), skill = await _setup(client, content="林远推开门。风很大。")
    inputs = {"hero": "苏晴", "基调": "沉重"}
    fake_llm.push("改写结果")
    resp = await client.post(
        f"/api/v1/chapters/{ch['id']}/rewrite",
        json={"op": "rewrite", "start": 0, "end": 5, "skill_id": skill["id"], "skill_inputs": inputs},
    )
    assert resp.status_code == 200
    assert "基调：沉重；重点人物：苏晴" in _system(fake_llm.calls[-1])
    logs = await _logs(client, base, ch)
    assert logs[0]["params"]["skill"]["inputs"] == inputs
    resp = await client.post(
        f"/api/v1/chapters/{ch['id']}/rewrite",
        json={"op": "rewrite", "start": 0, "end": 5, "skill_id": skill["id"], "skill_inputs": {"基调": "沉重"}},
    )
    assert resp.status_code == 400 and "重点人物" in resp.json()["detail"]
    fake_llm.push("润色后。")
    resp = await client.post(
        f"/api/v1/chapters/{ch['id']}/polish", json={"skill_id": skill["id"], "skill_inputs": inputs}
    )
    task, _ = await run_task(client, resp)
    assert task["status"] == "succeeded" and "重点人物：苏晴" in _system(fake_llm.calls[-1])


async def test_batch_uses_same_inputs_for_every_chapter(client, fake_llm):
    base, (c1, c2), skill = await _setup(client, chapters=2)
    fake_llm.push("一")
    fake_llm.push("二")
    body = {"start_chapter_id": c1["id"], "end_chapter_id": c2["id"], "auto_continue": False, "review_every": 1,
            "skill_id": skill["id"], "skill_inputs": {"hero": "林远", "基调": "沉重"}}
    bad = await client.post(f"{base}/batch-generate", json={**body, "skill_inputs": {"基调": "沉重"}})
    assert bad.status_code == 400
    resp = await client.post(f"{base}/batch-generate", json=body)
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["id"]
    assert (await wait_task(client, task_id, statuses=("paused", "failed")))["status"] == "paused"
    await client.patch(f"{SKILLS}/{skill['id']}", json={"inputs": [HERO], "body": "第二版"})
    assert (await client.post(f"/api/v1/tasks/{task_id}/resume")).status_code == 202
    assert (await wait_task(client, task_id))["status"] == "succeeded"
    prompts = [_system(c) for c in fake_llm.calls]
    assert len(prompts) == 2 and all("基调：沉重；重点人物：林远" in p for p in prompts)
    assert (await _logs(client, base, c2))[0]["params"]["skill"]["inputs"] == {"基调": "沉重", "hero": "林远"}


# ---------------- 智能体 ----------------


async def _agent(client):
    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    conv = (await client.post(base + "/conversations", json={})).json()
    return project, base + f"/conversations/{conv['id']}"


async def _send(client, url, text="请帮忙", **kw):
    response = await client.post(url + "/messages", json={"content": text, **kw})
    assert response.status_code == 202, response.text
    return response.json()["task"]["id"]


def _forced(fake_llm) -> list[str]:
    messages = fake_llm.tool_calls[-1]["messages"]
    return [m["content"] for m in messages if "用户为本轮指定了 Skill「定调」" in m["content"]]


async def test_agent_load_skill_trigger_and_manual_inputs(client, fake_llm):
    _, url = await _agent(client)
    resp = await client.post(SKILLS, json={"slug": "tone", "name": "定调", "body": BODY, "inputs": [TONE, HERO],
                                           "triggers": ["定调"]})
    skill = resp.json()
    # load_skill：未填充正文 + 输入项定义 + 代填说明
    fake_llm.push_tools(
        [("tool_call", {"id": "a", "name": "load_skill", "arguments": json.dumps({"slug": "tone"})})]
    )
    fake_llm.push_tools([("text", {"text": "好"})])
    assert (await wait_task(client, await _send(client, url)))["status"] == "succeeded"
    result = next(
        json.loads(m["content"])["data"] for m in (await client.get(url)).json()["messages"] if m["role"] == "tool"
    )
    assert result["body"] == BODY and result["instructions"] == si.AGENT_FILL_INSTRUCTIONS
    assert [d["name"] for d in result["inputs"]] == ["基调", "hero"]
    assert result["inputs"][0]["options"] == ["轻松", "沉重"]

    # 触发词：正文不填充，附输入项定义与代填说明
    fake_llm.push_tools([("text", {"text": "好"})])
    assert (await wait_task(client, await _send(client, url, "/定调 写一段")))["status"] == "succeeded"
    forced = _forced(fake_llm)
    assert forced and BODY in forced[0] and "【该 Skill 的输入项】" in forced[0]
    assert "{{ hero }}｜重点人物｜单行文本｜必填" in forced[0] and si.AGENT_FILL_INSTRUCTIONS in forced[0]

    # 手动指定：按请求输入填充；缺必填 400
    resp = await client.post(url + "/messages", json={"content": "x", "skill_id": skill["id"]})
    assert resp.status_code == 400 and "重点人物" in resp.json()["detail"]
    fake_llm.push_tools([("text", {"text": "好"})])
    task_id = await _send(client, url, "写", skill_id=skill["id"], skill_inputs={"hero": "林远"})
    assert (await wait_task(client, task_id))["status"] == "succeeded"
    forced = _forced(fake_llm)
    assert "基调：轻松；重点人物：林远" in forced[-1] and "【该 Skill 的输入项】" not in forced[-1]
    user_message = [m for m in (await client.get(url)).json()["messages"] if m["role"] == "user"][-1]
    assert user_message["skill"] == {"id": skill["id"], "name": "定调"}


async def test_agent_start_task_skill_inputs(client, app):
    from app.models import Project
    from app.services.agent.tools import get_tool
    from app.services.agent.tools.base import ToolCtx

    base, (ch,), skill = await _setup(client)
    tool = get_tool("start_task")
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, int(base.rsplit("/", 1)[1]))
        ctx = ToolCtx(db, project, project.user_id, app.state.task_engine.ctx)
        missing = await tool.preview(
            ctx, {"kind": "chapter.generate", "params": {"chapter_id": ch["id"], "skill_id": skill["id"]}}
        )
        assert "error" in missing and "重点人物" in missing["error"]
        params = {"chapter_id": ch["id"], "skill_id": skill["id"], "skill_inputs": [{"name": "hero", "value": "林远"}]}
        first = await tool.preview(ctx, {"kind": "chapter.generate", "params": params})
        assert first["skill"]["inputs"] == {"基调": "轻松", "hero": "林远"}
        assert any(d["label"] == "Skill 输入" and d["after"] == {"hero": "林远"} for d in first["diff"])
        await db.rollback()
        params["skill_inputs"] = [{"name": "hero", "value": "苏晴"}]
        second = await tool.preview(ctx, {"kind": "chapter.generate", "params": params})
        assert second["skill"]["hash"] != first["skill"]["hash"]
        json.dumps(second)
        await db.rollback()
        # 未知标识同样是工具错误
        params["skill_inputs"] = [{"name": "nope", "value": "x"}]
        unknown = await tool.preview(ctx, {"kind": "chapter.generate", "params": params})
        assert "未知的输入项" in unknown["error"]
        # 同一标识给了两次：不静默覆盖，返回工具错误
        params["skill_inputs"] = [{"name": "hero", "value": "甲"}, {"name": "hero", "value": "乙"}]
        duplicated = await tool.preview(ctx, {"kind": "chapter.generate", "params": params})
        assert "标识重复" in duplicated["error"]


def test_start_task_skill_inputs_schema_is_gemini_friendly():
    from app.ai.providers.gemini import _gemini_schema
    from app.services.agent.tools import get_tool

    schema = _gemini_schema(get_tool("start_task").parameters, tool_name="start_task")
    field = schema["properties"]["params"]["properties"]["skill_inputs"]
    assert field["type"] == "array" and set(field["items"]["properties"]) == {"name", "value"}


# ---------------- SKILL.md 导入导出 ----------------


async def _import(client, data: bytes):
    return await client.post(f"{SKILLS}/import", files={"file": ("SKILL.md", data)}, data={"scope": "mine"})


async def test_skill_md_inputs_roundtrip_and_rejects_invalid(client):
    await register(client)
    help_hero = dict(HERO, help="主视角人物", default="林远")
    skill = await _skill(client, inputs=[TONE, help_hero, {"name": "notes", "label": "备注", "kind": "textarea"}])
    resp = await client.get(f"{SKILLS}/{skill['id']}/export")
    assert resp.status_code == 200 and "inputs:" in resp.text
    await client.delete(f"{SKILLS}/{skill['id']}")
    again = await _import(client, resp.content)
    assert again.status_code == 201, again.text
    assert again.json()["inputs"] == skill["inputs"] and again.json()["body"] == skill["body"]
    # 没有输入项时不写 inputs
    plain = await _skill(client, slug="plain", inputs=[])
    assert "inputs" not in (await client.get(f"{SKILLS}/{plain['id']}/export")).text

    head = "---\nname: bad\ndescription: d\n"
    bad = [
        "inputs: abc\n",
        "inputs:\n  - name: [a, b]\n    label: x\n",
        "inputs:\n  - name: a\n    label: x\n    required: 'yes'\n",
        "inputs:\n  - name: a\n    label: x\n    extra: 1\n",
        "inputs:\n  - name: a\n    label: x\n    kind: select\n    options: [甲, 乙]\n    default: 丙\n",
        "inputs:\n  - name: a\n    label: x\n    options: [[1], 2]\n",
        "inputs:\n" + "".join(f"  - {{name: n{i}, label: x}}\n" for i in range(21)),
    ]
    for item in bad:
        resp = await _import(client, (head + item + "---\n正文").encode())
        assert resp.status_code == 400, (item, resp.text)
        assert resp.json()["detail"].startswith("Skill 导入失败")
    # YAML 别名炸弹放在 inputs 里同样被拒绝
    lines = ["---", "name: bomb", "description: d", 'a0: &a0 ["xxxxxxxxxx", "xxxxxxxxxx"]']
    lines += [f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]" for i in range(1, 30)]
    lines += ["inputs:", "  - name: a", "    label: *a29", "---", "正文"]
    resp = await _import(client, "\n".join(lines).encode())
    assert resp.status_code == 400 and "inputs" in resp.json()["detail"]


# ---------------- 场景 AI 与区块改写 ----------------


async def test_scene_generate_and_section_rewrite_fill_inputs(client, fake_llm):
    from tests.test_rich_document_api import KEY
    from tests.test_rich_document_section_ai import _setup as section_setup

    _project, url, scene, _document_url, request = await section_setup(client)
    skill = await _skill(client)
    inputs = {"hero": "苏晴", "基调": "沉重"}
    path = f"{url}/scenes/{scene['id']}/sections/{KEY}/rewrite"
    bad = await client.post(path, json={**request, "skill_id": skill["id"], "skill_inputs": {"基调": "欢快"}})
    assert bad.status_code == 400 and not fake_llm.calls
    fake_llm.push("改好🙂")
    resp = await client.post(path, json={**request, "skill_id": skill["id"], "skill_inputs": inputs})
    assert resp.status_code == 200 and "基调：沉重；重点人物：苏晴" in _system(fake_llm.calls[-1])

    revisions = {
        f"expected_{key}": value
        for key, value in (await client.get(f"{url}/scenes/{scene['id']}/document")).json().items()
        if key in ("content_revision", "planning_revision")
    }
    fake_llm.push(AIProviderError("首次失败"))
    task, _ = await run_task(client, await client.post(
        f"{url}/scenes/{scene['id']}/generate", json={**revisions, "skill_id": skill["id"], "skill_inputs": inputs},
    ))
    assert task["status"] == "failed", task
    assert "基调：沉重；重点人物：苏晴" in _system(fake_llm.calls[-1])
    await client.patch(f"{SKILLS}/{skill['id']}", json={"body": "第二版", "inputs": []})
    fake_llm.push("重试成功")
    retried, _ = await run_task(client, await client.post(f"/api/v1/tasks/{task['id']}/retry"))
    # 场景任务成功后可能进入待审核；这里只关心重试沿用的快照
    assert retried["status"] in ("succeeded", "paused") and retried["retry_of_id"] == task["id"]
    assert "基调：沉重；重点人物：苏晴" in _system(fake_llm.calls[-1]) and "第二版" not in _system(fake_llm.calls[-1])
