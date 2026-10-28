"""提示词模板：覆盖生效 / 停用 / 恢复默认、保存校验（带位置）、沙箱、预览、默认已更新与三方对比、
历史版本、导入导出、隔离。"""

import dataclasses
import json

import pytest

from app.ai.prompts import registry
from app.ai.templating import TemplateVar
from app.services import prompt_service
from tests.helpers import create_ai_config, create_project, register, run_task

API = "/api/v1/prompt-templates"
GEN = f"{API}/chapter.generate"


def _default(key: str) -> tuple[str, str]:
    tpl = registry.TEMPLATES[key]
    return tpl.system, tpl.user


async def _setup(client):
    await register(client)
    await create_ai_config(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    ch = (await client.post(f"{base}/chapters", json={"title": "第一章", "content": ""})).json()
    return base, ch


async def _generate(client, ch, fake_llm, text="正文"):
    fake_llm.push(text)
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={}))
    assert task["status"] == "succeeded", task
    return fake_llm.calls[-1]["messages"]


async def _last_log(client, base, ch):
    logs = (await client.get(f"{base}/chapters/{ch['id']}/generation-logs")).json()
    return (await client.get(f"{base}/chapters/{ch['id']}/generation-logs/{logs[0]['id']}")).json()


async def test_list_and_detail(client):
    await register(client)
    items = (await client.get(API)).json()
    assert [i["key"] for i in items] == list(registry.TEMPLATE_KEYS) and len(items) == 30
    assert not any(i["overridden"] for i in items)
    detail = (await client.get(f"{API}/analysis.chapter")).json()
    system, user = _default("analysis.chapter")
    assert (detail["default_system"], detail["default_user"]) == (system, user)
    assert detail["override"] is None and detail["default_fingerprint"] == registry.fingerprint(system, user)
    locked = {b["name"]: b["value"] for b in detail["locked"]}
    assert set(locked) == {"output_schema", "json_rule"} and "【输出 JSON 结构】" in locked["output_schema"]
    names = {v["name"]: v for v in detail["variables"]}
    assert names["json_rule"]["locked"] and names["content"]["required"]
    assert (await client.get(f"{API}/nope.key")).status_code == 404


async def test_override_used_then_disable_enable_and_reset(client, fake_llm):
    base, ch = await _setup(client)
    system, user = _default("chapter.generate")
    resp = await client.put(GEN, json={"system": system, "user": user + "\n【标记】覆盖生效", "note": "加标记"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["overridden"] and resp.json()["enabled"] and not resp.json()["outdated"]

    messages = await _generate(client, ch, fake_llm)
    assert messages[-1]["content"].endswith("【标记】覆盖生效")
    log = await _last_log(client, base, ch)
    versions = (await client.get(f"{GEN}/versions")).json()
    assert log["params"]["prompt"] == {
        "key": "chapter.generate",
        "source": "override",
        "override_version_id": versions[0]["id"],
    }

    # 停用：回到系统默认，覆盖内容保留
    resp = await client.patch(GEN, json={"enabled": False})
    assert resp.status_code == 200 and resp.json()["enabled"] is False
    assert resp.json()["override"]["user"].endswith("覆盖生效")
    messages = await _generate(client, ch, fake_llm)
    assert "【标记】" not in messages[-1]["content"]
    assert (await _last_log(client, base, ch))["params"]["prompt"]["source"] == "default"

    # 重新启用：恢复覆盖
    await client.patch(GEN, json={"enabled": True})
    assert (await _generate(client, ch, fake_llm))[-1]["content"].endswith("覆盖生效")

    # 恢复默认：删除覆盖及历史
    assert (await client.delete(GEN)).status_code == 204
    assert (await client.get(GEN)).json()["override"] is None
    assert (await client.get(f"{GEN}/versions")).status_code == 404
    assert "【标记】" not in (await _generate(client, ch, fake_llm))[-1]["content"]


async def test_override_continue_and_wizard_and_analysis(client, fake_llm):
    base, ch = await _setup(client)
    sys_c, user_c = _default("chapter.continue")
    assert (
        await client.put(f"{API}/chapter.continue", json={"system": "", "user": "续写标记" + user_c})
    ).status_code == 200
    fake_llm.push("短")
    fake_llm.push("续写内容")
    fake_llm.push("")
    resp = await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 300, "auto_continue": True})
    task, _ = await run_task(client, resp)
    assert task["status"] == "succeeded"
    assert fake_llm.calls[1]["messages"][-1]["content"].startswith("续写标记")

    sys_w, user_w = _default("wizard.world")
    await client.put(f"{API}/wizard.world", json={"system": "世界观系统标记", "user": user_w})
    fake_llm.push('[{"title": "设定", "content": "内容"}]')
    task, _ = await run_task(client, await client.post(f"{base}/wizard/world", json={}))
    assert task["status"] == "succeeded"
    assert fake_llm.calls[-1]["messages"][0] == {"role": "system", "content": "世界观系统标记"}


async def test_continue_override_render_failure_keeps_generated_text(client, fake_llm):
    # 合法但运行时渲染失败的续写覆盖（访问不存在的属性）：只记警告，首轮正文照常落库
    base, ch = await _setup(client)
    _, user_c = _default("chapter.continue")
    resp = await client.put(f"{API}/chapter.continue", json={"user": user_c + "{{ current_words.nope }}"})
    assert resp.status_code == 200, resp.text
    fake_llm.push("短正文")
    resp = await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 300, "auto_continue": True})
    task, events = await run_task(client, resp)
    assert task["status"] == "succeeded", task
    done = [d for e, d in events if e == "done"][0]
    assert done["result"]["warning"].startswith("自动续写失败：") and "模板渲染失败" in done["result"]["warning"]
    assert (await client.get(f"{base}/chapters/{ch['id']}")).json()["content"] == "短正文"
    assert len(fake_llm.calls) == 1


def _issues(resp):
    assert resp.status_code == 400, resp.text
    return resp.json()["errors"]


async def test_invalid_templates_rejected_with_position(client):
    await register(client)
    system, user = _default("chapter.polish")
    # 语法错误：给出段与行
    resp = await client.put(f"{API}/chapter.polish", json={"system": system, "user": user + "\n{% if style %}"})
    errors = _issues(resp)
    assert errors[0]["part"] == "user" and errors[0]["line"] == user.count("\n") + 2
    assert "语法错误" in errors[0]["message"] and "用户段第" in resp.json()["detail"]
    # 未知变量
    errors = _issues(await client.put(f"{API}/chapter.polish", json={"system": "{{ secret }}", "user": user}))
    assert errors == [{"part": "system", "line": 1, "message": "未知变量：secret"}]
    # 删掉必需变量
    errors = _issues(
        await client.put(f"{API}/chapter.polish", json={"system": system, "user": user.replace("{{ content }}", "")})
    )
    assert errors[0]["message"] == "缺少必需变量 {{ content }}"
    # 删掉锁定变量 / 放进 if / 出现两次
    _, outline_user = _default("wizard.outline")
    no_format = outline_user.replace("{{ output_format }}", "")
    assert "必须保留" in _issues(await client.put(f"{API}/wizard.outline", json={"user": no_format}))[0]["message"]
    in_if = outline_user.replace("{{ output_format }}", "{% if world %}{{ output_format }}{% endif %}")
    assert "不能放进 if / for" in _issues(await client.put(f"{API}/wizard.outline", json={"user": in_if}))[0]["message"]
    twice = outline_user + "\n{{ output_format }}"
    assert "只能出现一次" in _issues(await client.put(f"{API}/wizard.outline", json={"user": twice}))[0]["message"]
    # range / 循环遍历非列表
    errors = _issues(
        await client.put(f"{API}/chapter.polish", json={"user": user + "{% for i in range(9) %}{% endfor %}"})
    )
    assert any("range" in e["message"] for e in errors)
    # 未保存
    assert (await client.get(f"{API}/chapter.polish")).json()["override"] is None


@pytest.mark.parametrize(
    "payload",
    [
        "{{ ''.__class__.__mro__ }}",
        "{{ content.__class__ }}",
        "{{ content.format() }}",
        "{{ 'x' * 999999999 }}",
        "{% set x = content %}",
    ],
)
async def test_sandbox_escape_rejected(client, payload):
    await register(client)
    _, user = _default("chapter.polish")
    assert (await client.put(f"{API}/chapter.polish", json={"user": user + payload})).status_code == 400


async def test_preview_renders_messages_with_locked_blocks(client):
    await register(client)
    system, user = _default("wizard.world")
    resp = await client.post(
        f"{API}/wizard.world/preview", json={"system": system, "user": user, "variables": {"instruction": "多写势力"}}
    )
    out = resp.json()
    assert resp.status_code == 200 and out["errors"] == []
    assert [m["role"] for m in out["messages"]] == ["system", "user"]
    content = out["messages"][1]["content"]
    assert "【作者额外要求】\n多写势力" in content
    for block in out["locked"]:
        assert block["value"] in content
    # 预览不落库；不合法时返回错误、不渲染
    assert (await client.get(f"{API}/wizard.world")).json()["override"] is None
    bad = (await client.post(f"{API}/wizard.world/preview", json={"user": "{{ nope }}"})).json()
    assert bad["messages"] == [] and {e["message"] for e in bad["errors"]} >= {"未知变量：nope"}
    # 锁定变量不能被请求体改写
    hacked = (
        await client.post(
            f"{API}/wizard.world/preview", json={"system": system, "user": user, "variables": {"json_rule": "随便写"}}
        )
    ).json()
    assert "随便写" not in hacked["messages"][1]["content"]


async def test_preview_variables_size_limited(client):
    await register(client)
    system, user = _default("wizard.world")
    big = {"instruction": "长" * 200_001}
    resp = await client.post(f"{API}/wizard.world/preview", json={"system": system, "user": user, "variables": big})
    assert resp.status_code == 422
    many = {f"v{i}": "x" for i in range(101)}
    resp = await client.post(f"{API}/wizard.world/preview", json={"system": system, "user": user, "variables": many})
    assert resp.status_code == 422


async def test_default_updated_marks_outdated_and_ack(client, monkeypatch):
    await register(client)
    old_system, old_user = _default("chapter.polish")
    await client.put(f"{API}/chapter.polish", json={"system": old_system, "user": old_user + "\n我的版本"})
    assert not (await client.get(f"{API}/chapter.polish")).json()["outdated"]

    new_tpl = dataclasses.replace(registry.TEMPLATES["chapter.polish"], user=old_user + "\n（新默认）")
    monkeypatch.setitem(registry.TEMPLATES, "chapter.polish", new_tpl)
    listed = {i["key"]: i for i in (await client.get(API)).json()}
    assert listed["chapter.polish"]["outdated"] is True
    detail = (await client.get(f"{API}/chapter.polish")).json()
    # 三方对比：旧默认（base）/ 新默认 / 我的版本
    assert detail["override"]["base_user"] == old_user
    assert detail["default_user"] == old_user + "\n（新默认）"
    assert detail["override"]["user"] == old_user + "\n我的版本"

    resp = await client.patch(f"{API}/chapter.polish", json={"ack_default": True})
    assert resp.json()["outdated"] is False and resp.json()["override"]["base_user"] == new_tpl.user
    assert resp.json()["override"]["user"] == old_user + "\n我的版本"


async def test_versions_pruned_and_restore(client, monkeypatch):
    await register(client)
    _, user = _default("chapter.polish")
    for i in range(22):
        assert (await client.put(f"{API}/chapter.polish", json={"user": f"{user}\n版本{i}"})).status_code == 200
    # 内容不变不新增版本
    await client.put(f"{API}/chapter.polish", json={"user": f"{user}\n版本21"})
    versions = (await client.get(f"{API}/chapter.polish/versions")).json()
    assert len(versions) == prompt_service.MAX_VERSIONS == 20
    assert versions[0]["user"].endswith("版本21") and versions[-1]["user"].endswith("版本2")

    oldest = versions[-1]
    resp = await client.post(f"{API}/chapter.polish/versions/{oldest['id']}/restore")
    assert resp.status_code == 200 and resp.json()["override"]["user"].endswith("版本2")
    versions = (await client.get(f"{API}/chapter.polish/versions")).json()
    assert len(versions) == 20 and versions[0]["note"] == f"回滚到版本 #{oldest['id']}"

    # 系统默认升级后旧版本不合法（新增了必需变量）→ 回滚 400
    tpl = registry.TEMPLATES["chapter.polish"]
    stricter = dataclasses.replace(
        tpl, variables=(*tpl.variables, TemplateVar("must_have", "新增的必需变量", required=True))
    )
    monkeypatch.setitem(registry.TEMPLATES, "chapter.polish", stricter)
    resp = await client.post(f"{API}/chapter.polish/versions/{versions[1]['id']}/restore")
    assert resp.status_code == 400 and "must_have" in resp.json()["detail"]
    assert (await client.post(f"{API}/chapter.polish/versions/999999/restore")).status_code == 404


async def test_export_import_roundtrip_and_validation(client, make_client):
    await register(client)
    _, user = _default("chapter.polish")
    await client.put(f"{API}/chapter.polish", json={"user": user + "\n导出标记"})
    await client.patch(f"{API}/chapter.polish", json={"enabled": False})
    exported = (await client.get(f"{API}/export")).json()
    assert exported["format"] == "ainovel.prompts" and exported["version"] == 1
    assert [(i["key"], i["enabled"]) for i in exported["items"]] == [("chapter.polish", False)]

    bob = make_client()
    await register(bob, "bob")
    sys_w, user_w = _default("wizard.world")
    data = {
        **exported,
        "items": [
            *exported["items"],
            {"key": "unknown.key", "system": "", "user": "x"},
            {"key": "wizard.world", "system": sys_w, "user": user_w},  # 与默认相同
            {"key": "chapter.rewrite", "system": "", "user": "{{ nope }}"},  # 不合法
        ],
    }
    result = (await bob.post(f"{API}/import", json={"data": data})).json()["items"]
    assert [(r["key"], r["status"]) for r in result] == [
        ("chapter.polish", "created"),
        ("unknown.key", "skipped"),
        ("wizard.world", "default"),
        ("chapter.rewrite", "invalid"),
    ]
    assert result[3]["errors"][0]["message"] == "未知变量：nope"
    detail = (await bob.get(f"{API}/chapter.polish")).json()
    assert detail["override"]["user"].endswith("导出标记") and detail["enabled"] is False
    assert (await bob.get(f"{API}/wizard.world")).json()["override"] is None
    # 再导入一次：内容相同
    again = (await bob.post(f"{API}/import", json={"data": exported})).json()["items"]
    assert again[0]["status"] == "unchanged"

    assert (await bob.post(f"{API}/import", json={"data": {"format": "x"}})).status_code == 400
    assert (await bob.post(f"{API}/import", json={"data": {**exported, "version": 9}})).status_code == 400
    big = {**exported, "items": [{"key": "chapter.polish", "user": "长" * 400_000}]}
    assert len(json.dumps(big, ensure_ascii=False).encode()) > 1024 * 1024
    resp = await bob.post(f"{API}/import", json={"data": big})
    assert resp.status_code == 400 and "1MB" in resp.json()["detail"]


async def test_overrides_isolated_between_users(client, make_client, fake_llm):
    base, ch = await _setup(client)
    _, user = _default("chapter.generate")
    await client.put(GEN, json={"user": user + "\nalice 专属"})
    alice_version = (await client.get(f"{GEN}/versions")).json()[0]["id"]

    bob = make_client()
    await register(bob, "bob")
    assert not {i["key"]: i for i in (await bob.get(API)).json()}["chapter.generate"]["overridden"]
    assert (await bob.get(f"{GEN}/versions")).status_code == 404
    assert (await bob.patch(GEN, json={"enabled": False})).status_code == 404
    assert (await bob.delete(GEN)).status_code == 404
    await bob.put(GEN, json={"user": user + "\nbob 的"})
    assert (await bob.post(f"{GEN}/versions/{alice_version}/restore")).status_code == 404
    assert (await bob.get(f"{API}/export")).json()["items"][0]["user"].endswith("bob 的")
    # alice 的生成仍用 alice 的覆盖
    assert (await _generate(client, ch, fake_llm))[-1]["content"].endswith("alice 专属")
