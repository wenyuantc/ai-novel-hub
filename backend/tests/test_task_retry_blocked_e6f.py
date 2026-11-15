"""E6-f（裁决 19）：TaskOut.retry_blocked_reason 与重试受理同一判定。

- 列表、详情（及其他经 ``task_service.to_out`` 的输出）给出同一只读字段；非 kb.index 任务恒为 None。
- 字段非空 ⇔ ``KbIndexHandler.validate_retry`` 在调用重新受理之前即拒绝（同一函数），HTTP 重试 409 且文案不变。
- 管理员任务监控使用独立白名单 schema（不复用 TaskOut），不涉及重试。
"""

import pytest

from app.core.errors import ConflictError
from app.models import Task
from app.services import kb_index_service, task_statistics_service
from app.services.task_engine.handlers.kb import KbIndexHandler
from tests.helpers import create_project, register

GRANT = {"invitation_id": "6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab", "revision": 1}


def _payloads(pid: int) -> dict[str, tuple[dict, str | None]]:
    doc = {"project_id": pid, "mode": "document", "document_id": 1}
    memory = {"project_id": pid, "mode": "memory"}
    rebuild = {"project_id": pid, "mode": "rebuild"}
    return {
        "author_rebuild": ({"request": rebuild}, None),
        "author_memory_legacy": ({"request": memory}, None),
        "analysis_memory": ({"request": memory, "_memory_ref_ids": [1], "_memory_analysis_id": 1}, "memory_bound"),
        "manual_memory": ({"request": memory, kb_index_service.MANUAL_MEMORY_KEY: [1], "_collaboration_grant": GRANT,
                           "_shared_embedding": {"mode": "keyword"}}, "memory_bound"),
        "collaborator_memory": ({"request": memory, "_collaboration_grant": GRANT}, "memory_bound"),
        "collaborator_rebuild": ({"request": rebuild, "_collaboration_grant": GRANT}, None),
        "legacy_cleanup": ({"request": doc, kb_index_service.LEGACY_CLEANUP_KEY: True}, "legacy_index"),
        "selection_without_grant": ({"request": doc, "_shared_embedding": {"mode": "keyword"}}, "binding_invalid"),
        "broken_grant": ({"request": doc, "_collaboration_grant": {"revision": "x"}}, "binding_invalid"),
        "broken_request": ({"request": "x"}, "request_invalid"),
    }


@pytest.mark.parametrize("case", list(_payloads(1)))
async def test_validate_retry_rejects_exactly_when_field_is_set(case, monkeypatch):
    payload, expected = _payloads(1)[case]
    assert kb_index_service.retry_blocked_reason(payload) == expected
    handler = KbIndexHandler()
    admitted = []

    async def fake_claim(self, db, ctx, *, user_id, kind, params):
        admitted.append(params)
        raise RuntimeError("reached admission")

    monkeypatch.setattr(KbIndexHandler, "validate_and_claim", fake_claim)
    task = Task(user_id=1, project_id=1, kind="kb.index", status="failed", input=payload)
    if expected is None:
        with pytest.raises(RuntimeError, match="reached admission"):
            await handler.validate_retry(None, None, user_id=1, task=task)
        assert admitted == [payload["request"]]
    else:
        with pytest.raises(ConflictError):
            await handler.validate_retry(None, None, user_id=1, task=task)
        assert admitted == []
    assert handler.retry_blocked_reason(task) == expected


async def test_task_out_exposes_reason_in_list_and_detail_and_retry_matches(app, client):
    user = await register(client, "retry-blocked-owner")
    project = await create_project(client)
    pid = project["id"]
    rows: dict[str, tuple[int, str, str | None]] = {}
    async with app.state.sessionmaker() as db:
        for case, (payload, expected) in _payloads(pid).items():
            if "_collaboration_grant" in payload or case == "author_rebuild":
                continue  # 协作行需真实邀请；作者重建会真实受理，另由单元用例覆盖
            task = Task(user_id=user["id"], project_id=pid, kind="kb.index", status="failed", title="剧情记忆向量",
                        resource_key=None, input=payload)
            db.add(task)
            await db.flush()
            await task_statistics_service.record_created(db, task)
            rows[case] = (task.id, "", expected)
        other = Task(user_id=user["id"], project_id=pid, kind="outline.continue", status="failed", title="大纲续写",
                     input={"request": {"project_id": pid}, kb_index_service.MANUAL_MEMORY_KEY: [1]})
        db.add(other)
        await db.flush()
        await task_statistics_service.record_created(db, other)
        await db.commit()
        keys = {case: (await db.get(Task, task_id)).statistics_key for case, (task_id, _, _) in rows.items()}
        other_id, other_key = other.id, other.statistics_key
    listed = await client.get("/api/v1/tasks", params={"project_id": pid})
    assert listed.status_code == 200, listed.text
    by_id = {item["id"]: item for item in listed.json()}
    for case, (task_id, _, expected) in rows.items():
        assert by_id[task_id]["retry_blocked_reason"] == expected, case
        detail = await client.get(f"/api/v1/tasks/{task_id}", params={"task_key": keys[case]})
        assert detail.status_code == 200 and detail.json()["retry_blocked_reason"] == expected, case
        retried = await client.post(f"/api/v1/tasks/{task_id}/retry", params={"task_key": keys[case]})
        if expected is not None:
            assert retried.status_code == 409, (case, retried.text)
        else:
            # 输入层不拒绝：进入重新受理（此处作者无向量配置等由受理本身决定），不是绑定类 409。
            assert "需先核对" not in retried.text and "旧版索引任务" not in retried.text, case
    # 非 kb.index 任务即使输入里带同名键也不判定。
    assert by_id[other_id]["retry_blocked_reason"] is None
    detail = await client.get(f"/api/v1/tasks/{other_id}", params={"task_key": other_key})
    assert detail.json()["retry_blocked_reason"] is None
