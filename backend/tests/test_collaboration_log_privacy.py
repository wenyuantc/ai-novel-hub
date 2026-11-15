"""生成记录按真实触发者隔离，备份只携带作者记录并修复引用。"""

import json

import pytest
from sqlalchemy import delete, select

from app.models import ChapterVersion, GenerationLog, SceneReviewPayloadChunk, SceneReviewProposal, Task, User
from app.services import rich_document_review
from tests.helpers import create_project, register
from tests.test_rich_document_api import KEY
from tests.test_rich_document_proposals import _pending


async def test_log_and_version_privacy_with_backup_roundtrip(app, client, make_client):
    owner = await register(client, "privacy-owner")
    guest_client = make_client()
    guest = await register(guest_client, "privacy-guest")
    admin_client = make_client()
    admin = await register(admin_client, "privacy-admin")
    project = await create_project(client)
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})).json()
    base = f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}"

    async with app.state.sessionmaker() as db:
        (await db.get(User, admin["id"])).is_admin = True
        logs = [GenerationLog(
            project_id=project["id"], chapter_id=chapter["id"], actor_user_id=actor,
            kind="generate", status="success", messages=[{"content": f"private-{label}"}],
        ) for label, actor in (("owner", owner["id"]), ("guest", guest["id"]), ("unknown", None))]
        db.add_all(logs)
        await db.flush()
        db.add_all([ChapterVersion(
            chapter_id=chapter["id"], seq=index, source="generate", content=f"正文{index}",
            generation_log_id=log.id,
        ) for index, log in enumerate(logs, 1)])
        await db.commit()
        log_ids = [log.id for log in logs]

    response = await client.get(f"{base}/generation-logs")
    assert response.status_code == 200 and [row["id"] for row in response.json()] == [log_ids[0]]
    assert (await client.get(f"{base}/generation-logs/{log_ids[1]}")).status_code == 404
    versions = (await client.get(f"{base}/versions")).json()
    assert {row["seq"]: row["generation_log_id"] for row in versions} == {1: log_ids[0], 2: None, 3: None}
    guest_version = next(row for row in versions if row["seq"] == 2)
    assert (await client.get(f"{base}/versions/{guest_version['id']}")).json()["generation_log_id"] is None
    admin_versions = (await admin_client.get(f"{base}/versions")).json()
    assert all(row["generation_log_id"] is None for row in admin_versions)
    patched = await admin_client.patch(f"{base}/versions/{guest_version['id']}", json={"note": "审核"})
    assert patched.status_code == 200 and patched.json()["generation_log_id"] is None

    backup_response = await client.get(f"/api/v1/projects/{project['id']}/backup", params={"logs": "true"})
    assert backup_response.status_code == 200
    backup = backup_response.json()
    assert [row["id"] for row in backup["tables"]["generation_logs"]] == [log_ids[0]]
    assert "actor_user_id" not in backup["tables"]["generation_logs"][0]
    assert {row["seq"]: row["generation_log_id"] for row in backup["tables"]["chapter_versions"]} == {
        1: log_ids[0], 2: None, 3: None,
    }
    estimate = (await client.get(f"/api/v1/projects/{project['id']}/backup/estimate")).json()
    assert estimate["generation_logs"]["rows"] == 1
    imported = await guest_client.post(
        "/api/v1/projects/import",
        files={"file": ("backup.json", json.dumps(backup, ensure_ascii=False).encode(), "application/json")},
    )
    assert imported.status_code == 201, imported.text
    imported_id = imported.json()["project"]["id"]
    async with app.state.sessionmaker() as db:
        log = await db.scalar(select(GenerationLog).where(GenerationLog.project_id == imported_id))
        assert log.actor_user_id == guest["id"]
        assert (await db.scalar(select(ChapterVersion).where(ChapterVersion.generation_log_id == log.id))).seq == 1


@pytest.mark.parametrize("legacy", [True, False])
async def test_rich_review_uses_task_actor_for_legacy_payload_and_rejects_spoof(app, client, fake_llm, legacy):
    _project, chapter, _url, scene, document_url, source, task, identity = await _pending(app, client, fake_llm)
    async with app.state.sessionmaker() as db:
        task_actor = (await db.get(Task, task["id"])).user_id
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        payload = await rich_document_review._read_payload(db, proposal)
        assert payload["log_values"]["actor_user_id"] == task_actor
        if legacy:
            payload["log_values"].pop("actor_user_id")
        else:
            payload["log_values"]["actor_user_id"] = task_actor + 100
        await db.execute(delete(SceneReviewPayloadChunk).where(SceneReviewPayloadChunk.proposal_id == proposal.id))
        await rich_document_review._write_payload(db, proposal, payload)
        await db.commit()
    response = await client.post(f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}/accept", json={
        "task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}], "confirm_format_loss": True,
    })
    if legacy:
        assert response.status_code == 200, response.text
        async with app.state.sessionmaker() as db:
            log = await db.scalar(select(GenerationLog).where(GenerationLog.chapter_id == chapter["id"]))
            assert log.actor_user_id == task_actor
    else:
        assert response.status_code == 409
        assert (await client.get(document_url)).json() == source
