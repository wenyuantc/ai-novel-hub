"""提示词组件库：CRUD 与修订号、上限、跨用户隔离；模板覆盖与 Skill 中的引用（校验、展开、快照、导出、工坊分享）；
引用完整性（删除 / 改标识 / 改坏内容）、使用关系、组件库导入导出。"""

import io
import zipfile

from sqlalchemy import update

from app.ai.prompts import registry
from app.models import User
from app.services import prompt_component_service
from tests.helpers import create_ai_config, create_project, register, run_task

API = "/api/v1/prompt-components"
PROMPTS = "/api/v1/prompt-templates"
GEN = f"{PROMPTS}/chapter.generate"
SKILLS = "/api/v1/skills"
GEN_NAME = registry.TEMPLATES["chapter.generate"].name


def _gen_default() -> tuple[str, str]:
    tpl = registry.TEMPLATES["chapter.generate"]
    return tpl.system, tpl.user


async def _component(client, slug="ban", content="【禁用词】不要写「仿佛」", **kw):
    resp = await client.post(API, json={"slug": slug, "name": kw.pop("name", "禁用词"), "content": content, **kw})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _save_override(client, system_extra="\n{{> ban }}"):
    system, user = _gen_default()
    return await client.put(GEN, json={"system": system + system_extra, "user": user})


async def _make_admin(app, username):
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.username == username).values(is_admin=True))
        await db.commit()


async def _setup_chapter(client):
    await create_ai_config(client)
    project = await create_project(client, style="")
    base = f"/api/v1/projects/{project['id']}"
    ch = (await client.post(f"{base}/chapters", json={"title": "第一章", "content": ""})).json()
    return base, ch


# ---------------------------------------------------------------- 组件库


async def test_crud_revision_search_and_validation(client):
    await register(client)
    comp = await _component(client, description="  说明  ")
    assert comp["revision"] == 1 and comp["usage_count"] == 0 and comp["usages"] == []
    assert comp["description"] == "说明" and comp["content_chars"] == len("【禁用词】不要写「仿佛」")
    # 标识冲突 / 非法 / 组件内引用组件
    assert (await client.post(API, json={"slug": "ban", "name": "x"})).status_code == 409
    assert (await client.post(API, json={"slug": "Bad Slug", "name": "x"})).status_code == 422
    resp = await client.post(API, json={"slug": "nest", "name": "x", "content": "a {{> ban }}"})
    assert resp.status_code == 400 and "不能引用其他组件" in resp.json()["detail"]
    assert (await client.post(API, json={"slug": "big", "name": "x", "content": "字" * 20001})).status_code == 422

    body = {"slug": "ban", "name": "禁用词表", "description": "", "content": "新内容", "revision": 1}
    resp = await client.put(f"{API}/{comp['id']}", json=body)
    assert resp.status_code == 200 and resp.json()["revision"] == 2 and resp.json()["content"] == "新内容"
    # 旧修订保存 → 409 stale_revision，不静默覆盖
    resp = await client.put(f"{API}/{comp['id']}", json={**body, "content": "旧草稿"})
    assert resp.status_code == 409 and resp.json()["code"] == "stale_revision"
    assert (await client.get(f"{API}/{comp['id']}")).json()["content"] == "新内容"
    resp = await client.put(f"{API}/{comp['id']}", json={**body, "content": "x {{> a }}", "revision": 2})
    assert resp.status_code == 400

    await _component(client, slug="tone", name="语气", content="克制")
    assert [c["slug"] for c in (await client.get(API)).json()] == ["ban", "tone"]
    assert [c["slug"] for c in (await client.get(API, params={"q": "语气"})).json()] == ["tone"]
    assert [c["slug"] for c in (await client.get(API, params={"q": "新内"})).json()] == ["ban"]
    assert (await client.get(API, params={"q": "%"})).json() == []
    assert (await client.delete(f"{API}/{comp['id']}")).status_code == 204
    assert (await client.get(f"{API}/{comp['id']}")).status_code == 404


async def test_quota(client, monkeypatch):
    await register(client)
    monkeypatch.setattr(prompt_component_service, "MAX_COMPONENTS", 2)
    await _component(client, slug="a")
    await _component(client, slug="b")
    resp = await client.post(API, json={"slug": "c", "name": "c"})
    assert resp.status_code == 400 and "最多保存 2 个组件" in resp.json()["detail"]


async def test_other_users_components_invisible_and_unreferenceable(client, make_client):
    await register(client)
    comp = await _component(client)
    bob = make_client()
    await register(bob, "bob")
    for method, kw in [("get", {}), ("delete", {}), ("put", {"json": {"slug": "ban", "name": "x", "revision": 1}})]:
        assert (await getattr(bob, method)(f"{API}/{comp['id']}", **kw)).status_code == 404
    assert (await bob.get(API)).json() == []
    # bob 引用 alice 的组件：不存在
    resp = await _save_override(bob)
    assert resp.status_code == 400 and "引用的组件不存在：ban" in resp.json()["detail"]
    resp = await bob.post(SKILLS, json={"slug": "s", "name": "s", "body": "{{> ban }}"})
    assert resp.status_code == 400 and "引用的组件不存在：ban" in resp.json()["detail"]
    # bob 可以有同名组件，互不影响
    await _component(bob, content="BOB 的内容")
    assert (await client.get(f"{API}/{comp['id']}")).json()["content"] == "【禁用词】不要写「仿佛」"


# ---------------------------------------------------------------- 模板覆盖


async def test_override_validation_reports_component_source(client):
    await register(client)
    system, user = _gen_default()
    # 不存在的组件：标明位置
    resp = await _save_override(client, "\n{{> nope }}")
    assert resp.status_code == 400
    line = system.count("\n") + 2
    assert {"part": "system", "line": line, "message": "引用的组件不存在：nope"} in resp.json()["errors"]
    # 组件内含未声明变量 / 禁止语法：错误带组件来源，行号是引用所在行
    await _component(client, slug="bad-var", content="第一行\n{{ undeclared_x }}")
    resp = await _save_override(client, "\n{{> bad-var }}")
    assert resp.status_code == 400
    err = resp.json()["errors"][0]
    assert err["line"] == line and err["message"] == "组件 bad-var：未知变量：undeclared_x"
    assert "组件 bad-var" in resp.json()["detail"]
    await _component(client, slug="bad-set", content="{% set x = 1 %}")
    resp = await _save_override(client, "\n{{> bad-set }}")
    assert resp.status_code == 400 and resp.json()["errors"][0]["message"].startswith("组件 bad-set：不支持")
    # 组件内容可以使用模板已声明的变量
    await _component(client, slug="uses-var", content="本章：{{ title }}")
    assert (await _save_override(client, "\n{{> uses-var }}")).status_code == 200
    # 预览：返回展开后的消息与引用的组件
    resp = await client.post(f"{GEN}/preview", json={"system": system + "\n{{> uses-var }}", "user": user})
    data = resp.json()
    assert data["errors"] == [] and [c["slug"] for c in data["components"]] == ["uses-var"]
    assert "{{>" not in data["messages"][0]["content"] and "本章：" in data["messages"][0]["content"]
    resp = await client.post(f"{GEN}/preview", json={"system": system + "\n{{> bad-var }}", "user": user})
    assert resp.json()["errors"][0]["message"] == "组件 bad-var：未知变量：undeclared_x"


async def test_override_and_skill_use_components_at_runtime_and_snapshot(client, fake_llm):
    await register(client)
    base, ch = await _setup_chapter(client)
    comp = await _component(client)
    assert (await _save_override(client)).status_code == 200
    detail = (await client.get(GEN)).json()
    assert detail["components"] == [{"id": comp["id"], "slug": "ban", "name": "禁用词"}]
    assert "{{> ban }}" in detail["override"]["system"]  # 库内存原文
    skill = (await client.post(SKILLS, json={"slug": "flow", "name": "流程", "body": "步骤：\n{{> ban }}"})).json()
    assert skill["components"] == [{"id": comp["id"], "slug": "ban", "name": "禁用词"}]
    assert skill["body"] == "步骤：\n{{> ban }}"

    from app.ai.providers import AIProviderError

    fake_llm.push(["部分", AIProviderError("模型调用失败：额度不足")])
    resp = await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"skill_id": skill["id"]})
    failed, _ = await run_task(client, resp)
    assert failed["status"] == "failed"
    system = fake_llm.calls[-1]["messages"][0]["content"]
    assert system.count("【禁用词】不要写「仿佛」") == 2 and "{{>" not in system

    # 修改组件：新任务立即使用新内容；修改前创建的任务重试仍用旧快照（覆盖则按运行时最新内容）
    put = {"slug": "ban", "name": "禁用词", "description": "", "content": "【禁用词】第二版", "revision": 1}
    assert (await client.put(f"{API}/{comp['id']}", json=put)).status_code == 200
    fake_llm.push("重试正文。")
    task, _ = await run_task(client, await client.post(f"/api/v1/tasks/{failed['id']}/retry"))
    assert task["status"] == "succeeded"
    system = fake_llm.calls[-1]["messages"][0]["content"]
    # Skill 部分是旧快照；覆盖（运行时读取）已是新内容
    assert "步骤：\n【禁用词】不要写「仿佛」" in system and system.endswith("【禁用词】第二版")
    assert system.count("第二版") == 1
    fake_llm.push("新任务正文。")
    task, _ = await run_task(
        client, await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"skill_id": skill["id"]})
    )
    assert task["status"] == "succeeded"
    system = fake_llm.calls[-1]["messages"][0]["content"]
    assert system.count("【禁用词】第二版") == 2 and "不要写「仿佛」" not in system


# ---------------------------------------------------------------- 引用完整性与使用关系


async def test_integrity_and_usages(client):
    await register(client)
    comp = await _component(client, content="通用要求")
    assert (await _save_override(client)).status_code == 200
    skill = (await client.post(SKILLS, json={"slug": "flow", "name": "流程", "body": "{{> ban }}"})).json()
    assert (await client.patch(GEN, json={"enabled": False})).status_code == 200

    detail = (await client.get(f"{API}/{comp['id']}")).json()
    assert detail["usage_count"] == 2
    assert detail["usages"] == [
        {"kind": "skill", "id": skill["id"], "key": None, "name": "流程", "enabled": None},
        {"kind": "override", "id": None, "key": "chapter.generate", "name": GEN_NAME, "enabled": False},
    ]
    assert (await client.get(API)).json()[0]["usage_count"] == 2

    resp = await client.delete(f"{API}/{comp['id']}")
    assert resp.status_code == 409 and resp.json()["code"] == "component_in_use"
    assert {u["kind"] for u in resp.json()["usages"]} == {"skill", "override"}
    body = {"slug": "renamed", "name": "禁用词", "description": "", "content": "通用要求", "revision": 1}
    resp = await client.put(f"{API}/{comp['id']}", json=body)
    assert resp.status_code == 409 and resp.json()["code"] == "component_in_use"
    # 改内容会让引用它的覆盖校验失败 → 400，列出受影响的覆盖
    resp = await client.put(f"{API}/{comp['id']}", json={**body, "slug": "ban", "content": "{{ nope_var }}"})
    assert resp.status_code == 400 and resp.json()["code"] == "component_breaks_usages"
    failure = resp.json()["failures"][0]
    assert failure["key"] == "chapter.generate" and "未知变量：nope_var" in failure["errors"][0]["message"]
    assert (await client.get(f"{API}/{comp['id']}")).json()["revision"] == 1

    # 移除引用后可删除
    system, user = _gen_default()
    assert (await client.put(GEN, json={"system": system + "\n其他", "user": user})).status_code == 200
    assert (await client.patch(f"{SKILLS}/{skill['id']}", json={"body": "纯文本"})).status_code == 200
    assert (await client.patch(f"{SKILLS}/{skill['id']}", json={"body": "{{> missing }}"})).status_code == 400
    assert (await client.get(f"{API}/{comp['id']}")).json()["usages"] == []
    assert (await client.delete(f"{API}/{comp['id']}")).status_code == 204


# ---------------------------------------------------------------- 离开作者本人时展开


async def test_exports_and_workshop_share_are_expanded(client, app):
    await register(client)
    await _component(client, content="组件正文A")
    assert (await _save_override(client)).status_code == 200
    exported = (await client.get(f"{PROMPTS}/export")).json()
    item = exported["items"][0]
    assert "{{>" not in item["system"] and item["system"].endswith("\n组件正文A")

    skill = (await client.post(SKILLS, json={"slug": "flow", "name": "流程", "body": "开头\n{{> ban }}"})).json()
    resp = await client.get(f"{SKILLS}/{skill['id']}/export")
    assert resp.status_code == 200 and "{{>" not in resp.text and "开头\n组件正文A" in resp.text
    await client.put(f"{SKILLS}/{skill['id']}/references", json={"references": [{"title": "r", "content": "c"}]})
    resp = await client.get(f"{SKILLS}/{skill['id']}/export")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        text = archive.read("flow/SKILL.md").decode()
    assert "{{>" not in text and "组件正文A" in text

    system, user = _gen_default()
    share = {
        "kind": "prompt",
        "name": "分享",
        "prompt_key": "chapter.generate",
        "category": "general",
        "content": {"system": system + "\n{{> ban }}", "user": user},
    }
    resp = await client.post("/api/v1/workshop/submissions", json=share)
    assert resp.status_code == 201, resp.text
    submission = (await client.get(f"/api/v1/workshop/submissions/{resp.json()['id']}")).json()
    assert submission["content"]["system"] == system + "\n组件正文A"


async def test_global_skill_cannot_reference_components(client, app):
    await register(client, "root")
    await _make_admin(app, "root")
    await _component(client)
    resp = await client.post(SKILLS, json={"scope": "global", "slug": "g", "name": "g", "body": "{{> ban }}"})
    assert resp.status_code == 400 and "全局 Skill 不能引用" in resp.json()["detail"]
    md = b"---\nname: imp\ndescription: ''\n---\n{{> ban }}\n"
    resp = await client.post(f"{SKILLS}/import", files={"file": ("SKILL.md", md)}, data={"scope": "global"})
    assert resp.status_code == 400 and "全局 Skill 不能引用" in resp.json()["detail"]
    resp = await client.post(f"{SKILLS}/import", files={"file": ("SKILL.md", md)}, data={"scope": "mine"})
    assert resp.status_code == 201 and resp.json()["components"][0]["slug"] == "ban"
    builtin = next(s for s in (await client.get(SKILLS)).json() if s["builtin_key"])
    resp = await client.patch(f"{SKILLS}/{builtin['id']}", json={"body": "{{> ban }}"})
    assert resp.status_code == 400


async def test_agent_load_skill_reads_expanded_body(client, fake_llm):
    import json

    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    conv = (await client.post(base + "/conversations", json={})).json()
    url = base + f"/conversations/{conv['id']}"
    await _component(client, content="组件里的流程")
    await client.post(SKILLS, json={"slug": "flow", "name": "流程", "body": "开头 {{> ban }}"})
    fake_llm.push_tools(
        [("tool_call", {"id": "a", "name": "load_skill", "arguments": json.dumps({"slug": "flow"})})]
    )
    fake_llm.push_tools([("text", {"text": "好"})])
    from tests.helpers import wait_task

    resp = await client.post(url + "/messages", json={"content": "读一下"})
    assert (await wait_task(client, resp.json()["task"]["id"]))["status"] == "succeeded"
    results = [
        json.loads(m["content"])["data"] for m in (await client.get(url)).json()["messages"] if m["role"] == "tool"
    ]
    assert results[0]["body"] == "开头 组件里的流程"


# ---------------------------------------------------------------- 组件库导入导出


async def test_component_library_import_export(client, make_client):
    await register(client)
    await _component(client, slug="ban", content="A")
    await _component(client, slug="tone", content="B")
    exported = (await client.get(f"{API}/export")).json()
    assert exported["format"] == "ainovel.prompt-components" and exported["version"] == 1
    assert [i["slug"] for i in exported["items"]] == ["ban", "tone"]

    bob = make_client()
    await register(bob, "bob")
    await _component(bob, slug="tone", content="旧内容")
    await _component(bob, slug="keep", content="不变")
    system, user = _gen_default()
    await _component(bob, slug="used", content="ok")
    assert (await bob.put(GEN, json={"system": system + "\n{{> used }}", "user": user})).status_code == 200
    data = {
        **exported,
        "items": [
            *exported["items"],
            {"slug": "keep", "name": "禁用词", "description": "", "content": "不变"},
            {"slug": "used", "name": "禁用词", "description": "", "content": "{{ nope_var }}"},
            {"slug": "nest", "name": "n", "content": "{{> ban }}"},
            {"slug": "Bad", "name": "n"},
            {"slug": "ban", "name": "重复", "content": "x"},
            "not-object",
        ],
    }
    resp = await bob.post(f"{API}/import", json={"data": data})
    assert resp.status_code == 200, resp.text
    statuses = [(i["slug"], i["status"]) for i in resp.json()["items"]]
    assert statuses == [
        ("ban", "created"),
        ("tone", "updated"),
        ("keep", "unchanged"),
        ("used", "invalid"),
        ("nest", "invalid"),
        ("Bad", "invalid"),
        ("ban", "invalid"),
        ("", "invalid"),
    ]
    assert resp.json()["items"][3]["failures"][0]["key"] == "chapter.generate"
    contents = {c["slug"]: c for c in (await bob.get(API)).json()}
    assert set(contents) == {"ban", "tone", "keep", "used"}
    assert contents["tone"]["revision"] == 2
    bad = await bob.post(f"{API}/import", json={"data": {"format": "x", "version": 1, "items": []}})
    assert bad.status_code == 400


async def test_runtime_missing_component_is_readable_error(client, app, fake_llm):
    """覆盖保存后组件因竞态消失：运行时给出可读错误，而不是把 `{{>` 交给 Jinja。"""
    from sqlalchemy import delete

    from app.models import PromptComponent

    await register(client)
    base, ch = await _setup_chapter(client)
    await _component(client)
    assert (await _save_override(client)).status_code == 200
    async with app.state.sessionmaker() as db:
        await db.execute(delete(PromptComponent))
        await db.commit()
    # 覆盖在任务运行时（build_job）读取 → 任务 failed，正文不变
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={}))
    assert task["status"] == "failed"
    assert "提示词模板「" in task["error"] and "引用的组件 ban 不存在，请检查" in task["error"]
