"""真实任务结束后只允许删除身份与结果可证明的单章收据。"""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.ai.providers import AIProviderError
from app.models import Chapter, ChapterVersion, GenerationLog, Task
from app.services import generation_service
from tests.helpers import Gate, create_ai_config, create_project, register, stream_task
from tests.test_collaboration_rewrite_identity import _http_clients


async def _completed(app, client, fake_llm, request=None):
    user = await register(client, f"receipt-{uuid4().hex[:12]}")
    project = await create_project(client)
    await create_ai_config(client)
    chapter = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
        "title": "章节", "content": "原始正文",
    })
    assert chapter.status_code == 201, chapter.text
    fake_llm.push("模型生成正文")
    response = await client.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json=request or {})
    assert response.status_code == 202, response.text
    identity = response.json()
    events = await stream_task(client, identity)
    assert events[-1][0] == "done" and events[-1][1]["task"]["status"] == "succeeded", events
    async with asyncio.timeout(15):
        while True:
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, identity["id"])
                if task.status == "succeeded" and task.execution_key is None:
                    return user, project, chapter.json(), identity
            await asyncio.sleep(0.02)


async def test_true_succeeded_single_chapter_task_can_be_deleted(app, fake_llm):
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        _user, project, chapter, task = await _completed(app, owner, fake_llm)
        deleted = await owner.delete(f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]})
        assert deleted.status_code == 204, deleted.text
        async with app.state.sessionmaker() as db:
            assert await db.get(Task, task["id"]) is None
            current = await db.get(Chapter, chapter["id"])
            assert current.project_id == project["id"] and current.content == "模型生成正文"


@pytest.mark.parametrize("damage", [
    "wrong_task_id", "boolean_task_id", "boolean_version", "wrong_project", "wrong_version",
    "bad_result_hash", "bad_version_hash", "bad_input_hash", "missing_hash", "wrong_round", "forged_claim",
    "old_without_proof", "changed_input", "changed_result", "changed_version", "version_other_project",
    "log_other_project", "boolean_result_id", "boolean_scene_id", "resigned_result", "resigned_version",
])
async def test_damaged_completion_receipt_cannot_be_deleted(app, fake_llm, damage):
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        _user, project, _chapter, task = await _completed(app, owner, fake_llm)
        other = await create_project(owner, title="另一作品") if damage in {
            "wrong_project", "version_other_project", "log_other_project",
        } else None
        other_chapter = None
        if damage == "version_other_project":
            response = await owner.post(f"/api/v1/projects/{other['id']}/chapters", json={"title": "别章"})
            assert response.status_code == 201, response.text
            other_chapter = response.json()
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, task["id"])
            checkpoint = deepcopy(row.checkpoint)
            receipt = checkpoint["_chapter_claim"]
            version_id = receipt["version_id"]
            log_id = (await db.get(ChapterVersion, version_id)).generation_log_id
            result_override = None
            if damage == "wrong_task_id":
                receipt["task_id"] += 1
            elif damage == "boolean_task_id":
                receipt["task_id"] = True
            elif damage == "boolean_version":
                receipt["version_id"] = True
            elif damage == "wrong_project":
                assert other is not None
                assert other["id"] != project["id"]
                receipt["project_id"] = other["id"]
            elif damage == "wrong_version":
                original = await db.scalar(select(ChapterVersion.id).where(
                    ChapterVersion.chapter_id == task["chapter_id"], ChapterVersion.id != version_id,
                ))
                assert original is not None
                receipt["version_id"] = original
            elif damage == "bad_result_hash":
                receipt["result_hash"] = "0" * 64
            elif damage == "bad_version_hash":
                receipt["version_hash"] = "0" * 64
            elif damage == "bad_input_hash":
                receipt["input_hash"] = "0" * 64
            elif damage == "missing_hash":
                receipt.pop("result_hash")
            elif damage == "old_without_proof":
                receipt["schema"] = 1
                for key in ("result_hash", "version_hash", "input_hash"):
                    receipt.pop(key)
            elif damage == "wrong_round":
                receipt["queued_at"] = "2020-01-01T00:00:00+00:00"
            elif damage == "forged_claim":
                receipt["claim_key"] = receipt["task_key"]
            elif damage == "changed_input":
                payload = deepcopy(row.input)
                payload["request"]["instruction"] = "后插入的伪造指令"
                row.input = payload
            elif damage == "changed_result":
                result = deepcopy(row.result)
                result["chapter"]["content"] = "替换的结果"
                row.result = result
            elif damage == "changed_version":
                (await db.get(ChapterVersion, version_id)).content = "替换的版本正文"
            elif damage == "version_other_project":
                (await db.get(ChapterVersion, version_id)).chapter_id = other_chapter["id"]
            elif damage == "log_other_project":
                (await db.get(GenerationLog, log_id)).project_id = other["id"]
            elif damage in {"boolean_result_id", "boolean_scene_id", "resigned_result"}:
                result_override = deepcopy(row.result)
                if damage == "boolean_result_id":
                    result_override["chapter"]["id"] = True
                elif damage == "boolean_scene_id":
                    result_override["scene_ids"][0] = True
                else:
                    result_override["chapter"]["content"] = "伪造的正文"
                receipt["result_hash"] = generation_service._receipt_digest(result_override)
            else:
                version = await db.get(ChapterVersion, version_id)
                version.content = "篡改后重新计算的版本正文"
                receipt["version_hash"] = generation_service._version_digest(version)
            # Python 的 True == 1；用 SQL 强制保存 JSON 中的布尔破损。
            values = {"checkpoint": checkpoint}
            if result_override is not None:
                values["result"] = result_override
            await db.execute(update(Task).where(Task.id == task["id"]).values(**values))
            await db.commit()
        denied = await owner.delete(f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]})
        assert denied.status_code == 409, denied.text
        async with app.state.sessionmaker() as db:
            assert await db.get(Task, task["id"]) is not None
            assert await db.get(ChapterVersion, version_id) is not None
            assert await db.get(GenerationLog, log_id) is not None


async def test_later_chapter_edits_do_not_invalidate_completed_receipt(app, fake_llm):
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        _user, project, chapter, task = await _completed(app, owner, fake_llm)
        edited = await owner.patch(f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}", json={
            "content": "作者后续编辑的正文",
        })
        assert edited.status_code == 200, edited.text
        deleted = await owner.delete(f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]})
        assert deleted.status_code == 204, deleted.text
        assert (await owner.get(f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}")).json()["content"] \
            == "作者后续编辑的正文"


async def test_completed_task_delete_does_not_release_new_chapter_claim(app, fake_llm):
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        _user, _project, chapter, previous = await _completed(app, owner, fake_llm)
        gate = Gate()
        fake_llm.push(["第二轮", gate, "完成"])
        response = await owner.post(f"/api/v1/chapters/{chapter['id']}/generate")
        assert response.status_code == 202, response.text
        current_task = response.json()
        try:
            await gate.wait_reached()
            async with app.state.sessionmaker() as db:
                current = await db.get(Chapter, chapter["id"])
                claim_key, claim_task_key = current.generation_claim_key, current.generation_claim_task_key
                assert current.status == "generating" and claim_task_key == current_task["task_key"]
            deleted = await owner.delete(
                f"/api/v1/tasks/{previous['id']}", params={"task_key": previous["task_key"]},
            )
            assert deleted.status_code == 204, deleted.text
            async with app.state.sessionmaker() as db:
                current = await db.get(Chapter, chapter["id"])
                assert (current.status, current.generation_claim_key, current.generation_claim_task_key) == (
                    "generating", claim_key, claim_task_key,
                )
            gate.release()
            assert (await stream_task(owner, current_task))[-1][1]["task"]["status"] == "succeeded"
        finally:
            gate.release()


async def test_client_cannot_choose_completion_receipt_identity(app, fake_llm):
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        forged = str(uuid4())
        _user, _project, _chapter, task = await _completed(app, owner, fake_llm, {
            "_chapter_claim": {"claim_key": forged}, "result_hash": "0" * 64,
            "checkpoint": {"_chapter_claim": {"task_key": forged}},
        })
        async with app.state.sessionmaker() as db:
            receipt = (await db.get(Task, task["id"])).checkpoint["_chapter_claim"]
            assert receipt["claim_key"] != forged and receipt["task_key"] == task["task_key"]
            assert receipt["result_hash"] != "0" * 64
        assert (await owner.delete(
            f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]},
        )).status_code == 204


async def test_completed_scene_target_receipt_can_be_deleted(app, fake_llm):
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        await register(owner, f"receipt-{uuid4().hex[:12]}")
        project = await create_project(owner)
        await create_ai_config(owner)
        chapter = await owner.post(f"/api/v1/projects/{project['id']}/chapters", json={
            "title": "章节", "content": "原始正文",
        })
        base = f"/api/v1/projects/{project['id']}/chapters/{chapter.json()['id']}"
        page = (await owner.get(f"{base}/scenes")).json()
        scene_id = page["items"][0]["id"]
        fake_llm.push("场景新正文")
        response = await owner.post(f"{base}/scenes/{scene_id}/generate", json={
            "expected_content_revision": chapter.json()["content_revision"],
            "expected_planning_revision": page["planning_revision"],
        })
        assert response.status_code == 202, response.text
        task = response.json()
        events = await stream_task(owner, task)
        assert events[-1][0] == "done" and events[-1][1]["result"]["scene"]["id"] == scene_id
        async with asyncio.timeout(15):
            while True:
                async with app.state.sessionmaker() as db:
                    row = await db.get(Task, task["id"])
                    if row.status == "succeeded" and row.execution_key is None:
                        break
                await asyncio.sleep(0.02)
        assert (await owner.delete(
            f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]},
        )).status_code == 204


async def test_completion_hash_includes_final_continuation_warning(app, fake_llm):
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        await register(owner, f"receipt-{uuid4().hex[:12]}")
        project = await create_project(owner)
        await create_ai_config(owner)
        chapter = await owner.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})
        fake_llm.push("短正文")
        fake_llm.push(AIProviderError("模拟续写失败"))
        created = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json={
            "target_words": 300, "auto_continue": True,
        })
        assert created.status_code == 202, created.text
        task = created.json()
        events = await stream_task(owner, task)
        assert events[-1][0] == "done" and "模拟续写失败" in events[-1][1]["result"]["warning"]
        async with asyncio.timeout(15):
            while True:
                async with app.state.sessionmaker() as db:
                    row = await db.get(Task, task["id"])
                    if row.status == "succeeded" and row.execution_key is None:
                        break
                await asyncio.sleep(0.02)
        deleted = await owner.delete(f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]})
        assert deleted.status_code == 204, deleted.text


async def test_success_after_admin_cancel_race_keeps_valid_receipt(app, client, fake_llm, monkeypatch):
    from tests.test_admin_task_cancellation import test_admin_cancel_preserves_real_atomic_save_boundary

    await test_admin_cancel_preserves_real_atomic_save_boundary(app, client, fake_llm, monkeypatch, "committed")
    async with asyncio.timeout(15):
        while True:
            async with app.state.sessionmaker() as db:
                row = await db.scalar(select(Task).where(Task.kind == "chapter.generate"))
                if row.status == "succeeded" and row.execution_key is None:
                    task_id, key = row.id, row.statistics_key
                    break
            await asyncio.sleep(0.02)
    response = await client.delete(f"/api/v1/tasks/{task_id}", params={"task_key": key})
    assert response.status_code == 204, response.text
