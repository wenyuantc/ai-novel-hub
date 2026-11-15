"""待审提案保留可信模型来源；公开预览不泄露内部配置身份。"""

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import GenerationLog, SceneReviewProposal
from app.services import rich_document_review
from tests.test_rich_document_api import KEY
from tests.test_rich_document_proposals import _pending


async def test_proposal_persists_model_binding_privately(app, client, fake_llm):
    project, _chapter, _url, scene, _document_url, _source, task, identity = await _pending(app, client, fake_llm)
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        payload = await rich_document_review._read_payload(db, proposal)
        binding = payload["_model_binding"]
        assert binding["project_id"] == project["id"] and binding["source"] == "actor"
        assert binding["actor_id"] == binding["project_owner_id"]
        assert binding["model_type"] == "text" and len(binding["config_fingerprint"]) == 64
    preview = await client.get(f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}", params={
        "task_key": identity["task_key"], "run_key": identity["run_key"],
    })
    assert preview.status_code == 200, preview.text
    assert preview.json()["scenes"][0]["scene_id"] == scene["id"]
    assert "_model_binding" not in preview.text and binding["config_fingerprint"] not in preview.text


@pytest.mark.parametrize("change", ["actor", "project", "owner", "config", "grant"])
async def test_proposal_with_wrong_model_binding_cannot_be_accepted(app, client, fake_llm, change):
    project, _chapter, _url, scene, document_url, source, task, identity = await _pending(app, client, fake_llm)
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        payload = await rich_document_review._read_payload(db, proposal)
        assert payload["_model_binding"]["project_id"] == project["id"]
        if change == "grant":
            payload["_model_binding"]["grant_identity"] = {"invitation_id": str(uuid4()), "revision": 1}
        else:
            key = {"actor": "actor_id", "project": "project_id", "owner": "project_owner_id",
                   "config": "config_id"}[change]
            payload["_model_binding"][key] += 100
        # 保存为旧版单行负载，验证坏来源身份不会被当作当前任务本人重新签发。
        proposal.payload = payload
        await db.commit()
    accepted = await client.post(f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}/accept", json={
        "task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}],
        "confirm_format_loss": True,
    })
    assert accepted.status_code in {400, 409}, accepted.text
    assert (await client.get(document_url)).json() == source
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        assert proposal.status == "pending"


async def test_proposal_keeps_original_source_after_config_deletion(app, client, fake_llm):
    _project, _chapter, _url, scene, document_url, _source, task, identity = await _pending(app, client, fake_llm)
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        payload = await rich_document_review._read_payload(db, proposal)
        binding = payload["_model_binding"]
    deleted = await client.delete(f"/api/v1/ai-configs/{binding['config_id']}")
    assert deleted.status_code == 204, deleted.text
    accepted = await client.post(f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}/accept", json={
        "task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}],
        "confirm_format_loss": True,
    })
    assert accepted.status_code == 200, accepted.text
    assert (await client.get(document_url)).json()["document"]["content"] == "完全不同的候选稿"
    async with app.state.sessionmaker() as db:
        log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == task["id"]))
        assert log.ai_config_id is None and log.model_source == "actor"
        assert log.model_config_owner_id == binding["config_owner_id"]


async def test_legacy_proposal_does_not_guess_model_source(app, client, fake_llm):
    _project, _chapter, _url, scene, _document_url, _source, task, identity = await _pending(app, client, fake_llm)
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        payload = await rich_document_review._read_payload(db, proposal)
        payload.pop("_model_binding")
        proposal.payload = payload
        await db.commit()
    accepted = await client.post(f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}/accept", json={
        "task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}],
        "confirm_format_loss": True,
    })
    assert accepted.status_code == 200, accepted.text
    async with app.state.sessionmaker() as db:
        log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == task["id"]))
        assert log.ai_config_id is None and log.model_source is None and log.model_config_owner_id is None
