"""向导从中断处继续：部分输出截断 / 续写开头清洗 / 接口校验 / 拼接后解析落库 / 链式继续。"""

import json

import pytest

from app.ai.providers import AIProviderError
from app.services.wizard_service import clean_continuation_head, trim_to_resume_point
from tests.helpers import create_ai_config, create_project, register, run_task

STREAM_FAILED = AIProviderError("模型调用失败：Upstream HTTP/2 stream failed")

FULL_OUTLINE = json.dumps(
    {
        "volumes": [
            {
                "volume_no": 1,
                "volume_title": "启程",
                "chapters": [
                    {"chapter_no": 1, "title": "出发", "summary": "离港"},
                    {"chapter_no": 2, "title": "异象", "summary": "遭遇异常信号"},
                    {"chapter_no": 3, "title": "接触", "summary": "首次接触"},
                ],
            }
        ]
    },
    ensure_ascii=False,
)
# 第 2 章写到一半时断开
CUT = FULL_OUTLINE.index('"summary": "遭遇') + 5
PARTIAL = FULL_OUTLINE[:CUT]
# 截断点：第 1 章对象结尾
RESUME_POINT = FULL_OUTLINE[: FULL_OUTLINE.index("}") + 1]
REST = FULL_OUTLINE[len(RESUME_POINT) :]


def test_trim_to_resume_point():
    assert trim_to_resume_point(PARTIAL) == RESUME_POINT
    assert trim_to_resume_point('{"volumes": [{"volume_no": 1') == ""
    assert trim_to_resume_point("") == ""


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        # 正常接续（以逗号开头）
        (REST, REST),
        # 代码块标记 + 漏掉逗号 → 去标记补逗号
        ("```json\n" + REST.lstrip(", "), "," + REST.lstrip(", ")),
        # 重复了第 1 章 → 去掉重复部分
        ('{"chapter_no": 1, "title": "出发", "summary": "离港"}' + REST, REST.lstrip()),
    ],
)
def test_clean_continuation_head(head, expected):
    assert clean_continuation_head(RESUME_POINT, head) == (expected, False)


def test_clean_continuation_head_restart():
    text, restarted = clean_continuation_head(RESUME_POINT, "```json\n" + FULL_OUTLINE)
    assert restarted and text == FULL_OUTLINE


async def _failed_outline_task(client, fake_llm) -> tuple[dict, str]:
    await register(client)
    await create_ai_config(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    fake_llm.push([PARTIAL[:20], PARTIAL[20:], STREAM_FAILED])
    task, _ = await run_task(
        client,
        await client.post(
            f"{base}/wizard/outline", json={"volume_count": 1, "chapters_per_volume": 3, "instruction": "节奏紧凑"}
        ),
    )
    assert task["status"] == "failed" and "stream failed" in task["error"]
    return task, base


async def test_resume_outline_after_stream_failure(client, fake_llm):
    old, base = await _failed_outline_task(client, fake_llm)
    assert (await client.get(f"/api/v1/tasks/{old['id']}/output")).json()["text"] == PARTIAL

    # 模型漏写逗号、带代码块标记：清洗后拼接
    fake_llm.push("```json\n" + REST.lstrip(", "))
    resp = await client.post(f"{base}/wizard/outline", json={"continue_from_task_id": old["id"], "volume_count": 9})
    task, events = await run_task(client, resp)
    assert task["status"] == "succeeded", task["error"]
    assert task["retry_of_id"] == old["id"]
    assert [i["title"] for i in events[-1][1]["result"]["items"]] == ["出发", "异象", "接触"]
    assert len((await client.get(f"{base}/outlines")).json()) == 3

    msgs = fake_llm.calls[-1]["messages"]
    assert msgs[-2] == {"role": "assistant", "content": RESUME_POINT}
    assert "继续输出" in msgs[-1]["content"]
    # 沿用原请求参数（忽略本次传的 volume_count），不重复原 user 段
    assert "共 1 卷" in msgs[-3]["content"] and "节奏紧凑" in msgs[-3]["content"]


async def test_resume_chain_after_second_failure(client, fake_llm):
    old, base = await _failed_outline_task(client, fake_llm)
    second_rest = REST[: REST.index("}") + 1]  # 只多写了第 2 章
    fake_llm.push([second_rest + "x" * 700, STREAM_FAILED])
    second, _ = await run_task(
        client, await client.post(f"{base}/wizard/outline", json={"continue_from_task_id": old["id"]})
    )
    assert second["status"] == "failed"
    text = (await client.get(f"/api/v1/tasks/{second['id']}/output")).json()["text"]
    assert text.startswith(RESUME_POINT + second_rest)

    fake_llm.push(REST[len(second_rest) :])
    third, events = await run_task(
        client, await client.post(f"{base}/wizard/outline", json={"continue_from_task_id": second["id"]})
    )
    assert third["status"] == "succeeded", third["error"]
    assert [i["title"] for i in events[-1][1]["result"]["items"]] == ["出发", "异象", "接触"]


async def test_resume_rejects_invalid_source(client, fake_llm):
    old, base = await _failed_outline_task(client, fake_llm)
    # 类型不同
    resp = await client.post(f"{base}/wizard/world", json={"continue_from_task_id": old["id"]})
    assert resp.status_code == 400 and "不存在" in resp.json()["detail"]
    # 不存在
    resp = await client.post(f"{base}/wizard/outline", json={"continue_from_task_id": 9999})
    assert resp.status_code == 400
    # 已成功的任务不能继续
    fake_llm.push(FULL_OUTLINE)
    ok, _ = await run_task(client, await client.post(f"{base}/wizard/outline"))
    resp = await client.post(f"{base}/wizard/outline", json={"continue_from_task_id": ok["id"]})
    assert resp.status_code == 400 and "失败" in resp.json()["detail"]
    # 没有完整对象的部分输出
    fake_llm.push(['{"volumes": [{"volume_no": 1', STREAM_FAILED])
    bare, _ = await run_task(client, await client.post(f"{base}/wizard/outline"))
    resp = await client.post(f"{base}/wizard/outline", json={"continue_from_task_id": bare["id"]})
    assert resp.status_code == 400 and "没有可续写" in resp.json()["detail"]


async def test_resume_rejects_other_users_task(client, make_client, fake_llm):
    old, _ = await _failed_outline_task(client, fake_llm)
    other = make_client()
    await register(other, username="other")
    await create_ai_config(other)
    p = await create_project(other)
    resp = await other.post(f"/api/v1/projects/{p['id']}/wizard/outline", json={"continue_from_task_id": old["id"]})
    assert resp.status_code == 400
