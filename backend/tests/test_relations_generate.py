"""批量关系构思：按所选 ID 生成只读草稿，确认后原子写入。"""

import json

import pytest
from sqlalchemy import func, select

from app.ai.providers import AIProviderError
from app.models import Character, GenerationLog, Relationship, RelationshipHistory, RelationshipType
from tests.helpers import Gate, create_ai_config, create_project, register, run_task, wait_task


async def setup(client, *, config=True, count=3):
    await register(client)
    if config:
        await create_ai_config(client)
    project = await create_project(client, title="同名者的旅程", premise="失散兄弟重逢")
    base = f"/api/v1/projects/{project['id']}"
    characters = []
    for i in range(count):
        response = await client.post(
            f"{base}/characters",
            json={"name": "林远" if i < 2 else "张三", "aliases": [f"代号{i}"], "personality": f"性格{i}"},
        )
        assert response.status_code == 201, response.text
        characters.append(response.json())
    return base, [c["id"] for c in characters]


async def counts(app):
    async with app.state.sessionmaker() as db:
        return [
            await db.scalar(select(func.count()).select_from(model))
            for model in (Character, Relationship, RelationshipType, RelationshipHistory, GenerationLog)
        ]


def relation(a, b, **kwargs):
    return {
        "a_id": a,
        "b_id": b,
        "role_a": "徒弟",
        "role_b": "师父",
        "category": "social",
        "intimacy": 60,
        "status": "complicated",
        "description": "同名者之间的授业情谊",
        **kwargs,
    }


async def test_generate_preview_then_confirm_selected(app, client, fake_llm):
    base, ids = await setup(client)
    await client.post(f"{base}/world", json={"title": "北境", "content": "宗门重誓约", "category": "rule"})
    before = await counts(app)
    fake_llm.push(
        json.dumps({"relationships": [relation(ids[0], ids[1]), relation(ids[0], ids[2]), relation(ids[1], ids[2])]})
    )
    response = await client.post(
        f"{base}/relationships/generate", json={"character_ids": ids, "instruction": "围绕成长与信任"}
    )
    assert response.status_code == 202, response.text
    assert (response.json()["kind"], response.json()["title"], response.json()["character_id"]) == (
        "relations.generate",
        "AI 生成关系",
        None,
    )
    task, events = await run_task(client, response)
    assert task["status"] == "succeeded", task
    result = events[-1][1]["result"]
    assert result["character_ids"] == ids
    assert await counts(app) == before
    first, second, _ = result["suggestions"]
    assert (first["subject_id"], first["target_id"], first["resolved"], first["subject_is_a"]) == (
        ids[0],
        ids[1],
        True,
        False,
    )
    assert (first["subject_name"], first["target_name"]) == ("林远", "林远")
    assert first["type"]["role_a"] == "师父" and first["status"] == "complicated"
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "最多 3 条" in prompt
    for i, cid in enumerate(ids):
        assert f"ID={cid}" in prompt and f"代号{i}" in prompt and f"性格{i}" in prompt
    assert all(text in prompt for text in ("同名者的旅程", "失散兄弟重逢", "宗门重誓约", "围绕成长与信任"))
    # 只勾选两条，其中一条编辑为自定义称呼；类型和历史直到此刻才写入。
    save = await client.post(
        f"{base}/relationships/batch",
        json={
            "items": [
                {"a_id": first["target_id"], "b_id": first["subject_id"], **first["type"], "source": "ai_generate"},
                {
                    "a_id": second["subject_id"],
                    "b_id": second["target_id"],
                    "role_a": "引路人",
                    "role_b": "同行者",
                    "intimacy": 71,
                    "description": "预览中编辑",
                    "source": "ai_generate",
                },
            ]
        },
    )
    assert save.status_code == 201, save.text
    assert len(save.json()) == 2
    view = (await client.get(f"{base}/relationships", params={"character_id": ids[0]})).json()
    assert {(r["other_id"], r["other_role"]) for r in view} == {(ids[1], "师父"), (ids[2], "同行者")}
    assert len((await client.get(f"{base}/relationship-graph")).json()["edges"]) == 2
    for rel in save.json():
        history = (await client.get(f"{base}/relationships/{rel['id']}/history")).json()
        assert [h["source"] for h in history] == ["ai_generate"]


@pytest.mark.parametrize(
    "body",
    [
        {"character_ids": [1]},
        {"character_ids": [1, 1]},
        {"character_ids": [0, 1]},
        {"character_ids": list(range(1, 22))},
        {"character_ids": [1, 2], "count": 0},
        {"character_ids": [1, 2], "count": 51},
        {"character_ids": [1, 2], "count": True},
        {"character_ids": [1, 2], "instruction": "字" * 2001},
    ],
)
async def test_generate_invalid_selection(client, fake_llm, body):
    base, _ = await setup(client)
    response = await client.post(f"{base}/relationships/generate", json=body)
    assert response.status_code == 422, response.text
    assert "请求参数校验失败" in response.json()["detail"]
    assert (await client.get("/api/v1/tasks")).json() == []
    assert fake_llm.calls == []


async def test_generate_requires_missing_pairs_and_ai(client, fake_llm):
    base, ids = await setup(client, config=False, count=2)
    response = await client.post(f"{base}/relationships/generate", json={"character_ids": ids})
    assert response.status_code == 400 and "尚未配置文本模型" in response.json()["detail"]
    await create_ai_config(client)
    await client.post(f"{base}/relationships", json=relation(*ids))
    response = await client.post(f"{base}/relationships/generate", json={"character_ids": ids})
    assert response.status_code == 400 and response.json()["detail"] == "所选角色之间已无缺失关系，无需生成"
    assert fake_llm.calls == []
    assert (await client.get("/api/v1/tasks")).json() == []


async def test_generate_marks_duplicate_self_unselected_existing_and_invalid_fields(app, client, fake_llm):
    base, ids = await setup(client, count=6)
    response = await client.post(f"{base}/relationships", json=relation(ids[0], ids[1]))
    existing_id = response.json()["id"]
    before = await counts(app)
    rows = [
        relation(ids[0], ids[2]),
        relation(ids[2], ids[0]),
        relation(ids[0], ids[0]),
        relation(ids[1], ids[0]),
        relation(ids[0], ids[5]),  # 此角色存在，但本次没有选
        relation("林远", ids[1]),
        relation(ids[0], ids[3], role_a=""),
        relation(ids[0], ids[4], intimacy=101),
        relation(ids[1], ids[2], category="invalid"),
        relation(ids[1], ids[3], status="invalid"),
        relation(ids[1], ids[4], intimacy=2.5),
        relation(True, ids[2]),
        "无效行",
    ]
    fake_llm.push(json.dumps({"relationships": rows}))
    task, events = await run_task(
        client, await client.post(f"{base}/relationships/generate", json={"character_ids": ids[:5], "count": 10})
    )
    assert task["status"] == "succeeded", task
    result = events[-1][1]["result"]
    suggestions = result["suggestions"]
    assert [s["resolved"] for s in suggestions] == [True] + [False] * 11
    assert "含反向" in suggestions[1]["reason"]
    assert suggestions[2]["reason"] == "不能与自己建立关系"
    assert suggestions[3]["existing_relationship_id"] == existing_id
    assert all(s["reason"] for s in suggestions[1:])
    assert result["warnings"] == ["已忽略格式不正确的关系建议"]
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert f"ID={ids[0]} ↔ ID={ids[1]}" in prompt and "同名者之间的授业情谊" in prompt
    assert await counts(app) == before


async def test_generation_rechecks_current_data_after_llm(app, client, fake_llm):
    base, ids = await setup(client, count=4)
    gate = Gate()
    fake_llm.push([gate, json.dumps({"relationships": [relation(ids[0], ids[1]), relation(ids[0], ids[3])]})])
    response = await client.post(f"{base}/relationships/generate", json={"character_ids": ids})
    await gate.wait_reached()
    await client.post(f"{base}/relationships", json=relation(ids[1], ids[0]))
    await client.delete(f"{base}/characters/{ids[3]}")
    before = await counts(app)
    gate.release()
    task, events = await run_task(client, response)
    assert task["status"] == "succeeded", task
    suggestions = events[-1][1]["result"]["suggestions"]
    assert [s["resolved"] for s in suggestions] == [False, False]
    assert suggestions[0]["reason"] == "两人之间已有关系"
    assert suggestions[1]["reason"] == "角色已被删除或不属于该项目"
    assert suggestions[1]["target_name"] == "张三"
    assert await counts(app) == before


@pytest.mark.parametrize(
    "invalid", [{"role_a": ["徒弟"]}, {"role_b": 42}, {"category": ["social"]}, {"status": ["normal"]}]
)
async def test_generated_relation_fields_keep_invalid_json_types_unsavable(app, client, fake_llm, invalid):
    base, ids = await setup(client, count=2)
    before = await counts(app)
    fake_llm.push(json.dumps({"relationships": [relation(*ids, **invalid)]}))
    task, events = await run_task(
        client, await client.post(f"{base}/relationships/generate", json={"character_ids": ids})
    )
    assert task["status"] == "succeeded"
    row = events[-1][1]["result"]["suggestions"][0]
    assert row["resolved"] is False and row["reason"]
    assert await counts(app) == before


async def test_preview_conflict_rolls_back_new_types_and_entire_batch(app, client, fake_llm):
    base, ids = await setup(client)
    fake_llm.push(json.dumps({"relationships": [relation(ids[0], ids[1]), relation(ids[0], ids[2])]}))
    task, _ = await run_task(client, await client.post(f"{base}/relationships/generate", json={"character_ids": ids}))
    assert task["status"] == "succeeded"
    await client.post(f"{base}/relationships", json=relation(ids[1], ids[0], description="别人先保存的内容"))
    before = await counts(app)
    save = await client.post(
        f"{base}/relationships/batch",
        json={
            "items": [
                relation(ids[0], ids[2], role_a="引路人", role_b="同行者", source="ai_generate"),
                relation(ids[0], ids[1], source="ai_generate"),
            ]
        },
    )
    assert save.status_code == 409 and save.json()["detail"] == "这两个角色之间已有关系"
    assert await counts(app) == before
    assert (await client.get(f"{base}/relationships")).json()[0]["description"] == "别人先保存的内容"


@pytest.mark.parametrize(
    ("output", "status", "error"),
    [
        ('{"relationships": []}', "succeeded", ""),
        ('{"relations": []}', "failed", "relationships 数组"),
        ('{"relationships": {}}', "failed", "relationships 数组"),
        ("[]", "failed", "relationships 数组"),
        ("不是JSON", "failed", "解析失败"),
        ("   ", "failed", "模型未返回任何内容"),
        (["部分输出", AIProviderError("模型调用失败：测试错误")], "failed", "模型调用失败：测试错误"),
    ],
)
async def test_empty_result_and_errors_never_write(app, client, fake_llm, output, status, error):
    base, ids = await setup(client)
    before = await counts(app)
    fake_llm.push(output)
    task, events = await run_task(
        client, await client.post(f"{base}/relationships/generate", json={"character_ids": ids})
    )
    assert task["status"] == status and error in task["error"]
    if status == "succeeded":
        assert events[-1][1]["result"]["suggestions"] == []
    assert await counts(app) == before


async def test_count_is_capped_and_all_20_identities_survive_long_cards(client, fake_llm):
    base, ids = await setup(client, count=20)
    for cid in ids:
        await client.patch(f"{base}/characters/{cid}", json={"profile": "长篇资料" * 2000})
    fake_llm.push(json.dumps({"relationships": [relation(ids[0], cid) for cid in ids[1:]]}))
    task, events = await run_task(
        client, await client.post(f"{base}/relationships/generate", json={"character_ids": ids, "count": 1})
    )
    assert task["status"] == "succeeded"
    result = events[-1][1]["result"]
    assert len(result["suggestions"]) == 1 and "18 条" in result["warnings"][0]
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "最多 1 条" in prompt
    for cid in ids:
        assert f"ID={cid}｜" in prompt
    assert len(prompt) < 25000


async def test_generate_uses_enabled_knowledge_and_honors_project_switch(client, fake_llm):
    base, ids = await setup(client, count=2)
    upload = await client.post(
        f"{base}/kb/documents",
        files={"file": ("门派资料.md", "# 天剑宗\n\n天剑宗弟子看重师承与誓约。".encode(), "text/markdown")},
    )
    assert upload.status_code == 202, upload.text
    indexed = await wait_task(client, upload.json()["task"]["id"])
    assert indexed["status"] == "succeeded"
    await client.patch(f"{base}/kb/settings", json={"min_score": 0})
    for enabled in (True, False):
        await client.patch(f"{base}/kb/settings", json={"auto_inject": enabled})
        fake_llm.push('{"relationships": []}')
        task, _ = await run_task(
            client,
            await client.post(
                f"{base}/relationships/generate", json={"character_ids": ids, "instruction": "天剑宗的师承"}
            ),
        )
        assert task["status"] == "succeeded"
        prompt = fake_llm.calls[-1]["messages"][-1]["content"]
        assert ("《门派资料.md》" in prompt) is enabled
        assert ("【参考资料（来自知识库，仅供参考；与项目设定冲突时以设定为准）】" in prompt) is enabled


async def test_queued_task_rechecks_missing_pairs_without_model_call(app, client, fake_llm):
    base, ids = await setup(client, count=2)
    await app.state.task_engine.stop()
    response = await client.post(f"{base}/relationships/generate", json={"character_ids": ids})
    assert response.status_code == 202
    await client.post(f"{base}/relationships", json=relation(*ids))
    await app.state.task_engine.start()
    task = await wait_task(client, response.json()["id"])
    assert task["status"] == "failed" and "已无缺失关系" in task["error"]
    assert fake_llm.calls == []


async def test_cancel_and_retry_keep_original_selection(app, client, fake_llm):
    base, ids = await setup(client)
    before = await counts(app)
    gate = Gate()
    fake_llm.push([gate, '{"relationships": []}'])
    response = await client.post(f"{base}/relationships/generate", json={"character_ids": ids, "count": 2})
    await gate.wait_reached()
    active = (await client.get("/api/v1/tasks", params={"kind": "relations.generate", "active": True})).json()
    assert [t["id"] for t in active] == [response.json()["id"]]
    assert (await client.post(f"/api/v1/tasks/{response.json()['id']}/cancel")).status_code == 200
    cancelled = await wait_task(client, response.json()["id"])
    assert cancelled["status"] == "cancelled"
    assert await counts(app) == before
    fake_llm.push('{"relationships": []}')
    retry = await client.post(f"/api/v1/tasks/{response.json()['id']}/retry")
    task, events = await run_task(client, retry)
    assert task["status"] == "succeeded" and task["retry_of_id"] == cancelled["id"]
    assert events[-1][1]["result"]["character_ids"] == ids
    assert await counts(app) == before


async def test_generate_project_character_config_and_task_permissions(client, make_client, fake_llm):
    base, ids = await setup(client)
    other_project = await create_project(client)
    outside = (
        await client.post(f"/api/v1/projects/{other_project['id']}/characters", json={"name": "项目外角色"})
    ).json()
    for cid in (outside["id"], 99999):
        response = await client.post(f"{base}/relationships/generate", json={"character_ids": [ids[0], cid]})
        assert response.status_code == 404 and response.json()["detail"] == "所选角色不存在或不属于该项目"
    fake_llm.push('{"relationships": []}')
    task, _ = await run_task(client, await client.post(f"{base}/relationships/generate", json={"character_ids": ids}))
    bob = make_client()
    await register(bob, "bob")
    config = await create_ai_config(bob)
    denied = await client.post(
        f"{base}/relationships/generate", json={"character_ids": ids, "ai_config_id": config["id"]}
    )
    assert denied.status_code == 404 and denied.json()["detail"] == "模型配置不存在"
    denied = await bob.post(f"{base}/relationships/generate", json={"character_ids": ids})
    assert denied.status_code == 404 and denied.json()["detail"] == "项目不存在"
    for path in ("", "/output", "/stream"):
        response = await bob.get(f"/api/v1/tasks/{task['id']}{path}")
        assert response.status_code == 404 and response.json()["detail"] == "任务不存在"
    assert len(fake_llm.calls) == 1


async def test_relations_generate_template_override_and_locked_preview(client, fake_llm):
    from app.ai.prompts import registry
    from app.ai.prompts.common import JSON_ONLY_RULE
    from app.ai.prompts.formats import RELATIONS_GENERATE_FORMAT

    base, ids = await setup(client)
    key = "relations.generate"
    template = registry.TEMPLATES[key]
    api = f"/api/v1/prompt-templates/{key}"
    override_user = template.user + "\n关系模板覆盖生效"
    response = await client.put(api, json={"system": template.system, "user": override_user})
    assert response.status_code == 200, response.text
    preview = await client.post(
        f"{api}/preview",
        json={
            "system": template.system,
            "user": override_user,
            "variables": {"json_rule": "不要JSON", "output_format": "伪造格式"},
        },
    )
    assert preview.status_code == 200, preview.text
    locked = {v["name"]: v["value"] for v in preview.json()["locked"]}
    assert locked == {"json_rule": JSON_ONLY_RULE, "output_format": RELATIONS_GENERATE_FORMAT}
    preview_text = preview.json()["messages"][-1]["content"]
    assert "不要JSON" not in preview_text and "伪造格式" not in preview_text
    assert RELATIONS_GENERATE_FORMAT in preview_text
    fake_llm.push('{"relationships": []}')
    task, _ = await run_task(client, await client.post(f"{base}/relationships/generate", json={"character_ids": ids}))
    assert task["status"] == "succeeded"
    assert fake_llm.calls[-1]["messages"][-1]["content"].endswith("关系模板覆盖生效")
    # 每个锁定字段都必须直接引用且恰好一次。
    invalid = await client.put(
        api, json={"system": template.system, "user": template.user.replace("{{ output_format }}", "")}
    )
    assert invalid.status_code == 400 and "锁定变量" in invalid.json()["detail"]


def test_relations_generate_template_empty_and_full_context():
    from app.ai.prompts import ProjectInfo, build_relations_generate_messages
    from app.ai.prompts.formats import RELATIONS_GENERATE_FORMAT

    base = dict(
        identities="ID=11｜甲\nID=12｜乙",
        characters_text="ID=11：性格内敛\nID=12：性格直爽",
        existing_pairs="无",
        count=1,
    )
    empty = build_relations_generate_messages(ProjectInfo(title="作品"), **base)[-1]["content"]
    assert "【世界观】" not in empty and "【参考资料" not in empty and "【作者额外要求】" not in empty
    assert "ID=11｜甲" in empty and "ID=12｜乙" in empty and RELATIONS_GENERATE_FORMAT in empty
    full = build_relations_generate_messages(
        ProjectInfo(title="作品"),
        **base,
        world_text="门派重誓约",
        relationships_text="已有故交",
        types_text="朋友 / 朋友",
        knowledge="地方志参考",
        instruction="围绕成长",
    )[-1]["content"]
    assert all(
        t in full
        for t in ("门派重誓约", "已有故交", "朋友 / 朋友", "地方志参考", "围绕成长", "与项目设定冲突时以设定为准")
    )
