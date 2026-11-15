"""单书备份仅携带作者可见内容，不迁移实例私有凭据。"""

import json

import pytest
from sqlalchemy import select

from app.models import AgentChange, AgentConversation, GenerationLog, Task
from app.services import personal_projection
from app.services.project_transfer import exporter
from tests.helpers import create_project, register


async def test_backup_recursively_removes_runtime_evidence_without_censoring_public_content(app, client, make_client):
    owner = await register(client, "backup-private-owner")
    guest_client = make_client()
    guest = await register(guest_client, "backup-private-guest")
    project = await create_project(client)
    sentinel = "SERVER-PRIVATE-MARKER"
    nested = {
        "_batch_attempt": sentinel,
        "_BatchCompletion": sentinel,
        "_batch_outcome_unknown": sentinel,
        "_analysisPending": sentinel,
        "_analysis_completion": sentinel,
        "rich_analysis": sentinel,
        "_externalExecution": sentinel,
        "_ExternalSnapshotMac": sentinel,
        "_agent_actor_token_version": sentinel,
        "_TaskActorTokenVersion": sentinel,
        "_WizardActorTokenVersion": sentinel,
        "_retry_source_key": sentinel,
        "taskKey": sentinel,
        "ApiKey": sentinel,
        "encryptedAPIKey": sentinel,
        "apiKeyEnc": sentinel,
        "configFingerprint": sentinel,
        "_model_binding": sentinel,
        "style": "作者保留的风格",
        "projectModel": "作者描述的项目模型",
        "guards": [{"name": "城门守卫"}],
        "_timeline_revision": 12,
        "preview": {"description": "保留预览", "guards": [{"table": "tasks", "pk": {"id": 1}, "hash": sentinel}]},
        "diff": [{"field": "tasks._analysis_completion", "before": sentinel, "after": sentinel},
                 {"field": "tasks._external.snapshot.mac", "before": sentinel, "after": sentinel},
                 {"field": "chapters.style", "before": "古典", "after": "清新"}],
        "double_encoded": json.dumps(json.dumps({"_analysisPending": sentinel})),
        "all_reserved": [{key: sentinel} for key in personal_projection._INTERNAL_FIELDS],
        "truncated_private": f'{{"_external_execution":"{sentinel}',
        "truncated_double": json.dumps(json.dumps({"_external_execution": sentinel}))[:-2],
        "truncated_triple": json.dumps(json.dumps(json.dumps({"_external_execution": sentinel})))[:-2],
        "truncated_source": json.dumps(json.dumps({"model_source": sentinel}))[:-2],
        "truncated_preview": {"preview": f'{{"guards":[{{"table":"tasks","hash":"{sentinel}'},
        "truncated_public": '{"story":"作者保留的未完文本',
        "truncated_encoded_public": json.dumps(json.dumps({"story": "作者的公开文字"}))[:-2],
    }
    deep_encoded = {"_external_execution": sentinel}
    for _ in range(9):
        deep_encoded = json.dumps(deep_encoded)
    nested["truncated_deep"] = deep_encoded[:-2]
    embedded = json.dumps({"payload": [nested]}, ensure_ascii=False)
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="succeeded",
                    checkpoint={"_analysis_completion": sentinel})
        conversation = AgentConversation(user_id=owner["id"], project_id=project["id"])
        db.add_all([task, conversation])
        await db.flush()
        db.add(AgentChange(
            project_id=project["id"], conversation_id=conversation.id, turn_task_id=task.id,
            tool_call_id="hidden-change", tool_name="test", kind="small", arguments={"secret": sentinel},
        ))
        db.add_all([
            GenerationLog(project_id=project["id"], actor_user_id=owner["id"], kind="generate", status="error",
                          model_source="actor", model_config_owner_id=owner["id"], provider="owner-provider",
                          model="owner-model", params={"payload": nested, "encoded": embedded},
                          messages=[{"role": "tool", "content": embedded}, {"role": "user", "content": "作者正文"}],
                          error=json.dumps({"_external_execution": sentinel, "message": "正常错误"})),
            GenerationLog(project_id=project["id"], actor_user_id=guest["id"], kind="generate", status="success",
                          provider="guest-provider", model="guest-model", params={"secret": "GUEST-PRIVATE"}),
        ])
        await db.commit()
        task_id, conversation_id = task.id, conversation.id

    response = await client.get(f"/api/v1/projects/{project['id']}/backup", params={"logs": "true"})
    assert response.status_code == 200, response.text
    backup = response.json()
    assert {"tasks", "agent_conversations", "agent_messages", "agent_changes"}.isdisjoint(backup["tables"])
    logs = backup["tables"]["generation_logs"]
    assert len(logs) == 1 and logs[0]["provider"] == "owner-provider" and logs[0]["model"] == "owner-model"
    assert sentinel not in response.text and "GUEST-PRIVATE" not in response.text
    assert logs[0]["params"]["payload"]["style"] == "作者保留的风格"
    assert logs[0]["params"]["payload"]["projectModel"] == "作者描述的项目模型"
    assert logs[0]["params"]["payload"]["guards"] == [{"name": "城门守卫"}]
    assert logs[0]["params"]["payload"]["_timeline_revision"] == 12
    assert logs[0]["params"]["payload"]["truncated_public"] == '{"story":"作者保留的未完文本'
    assert logs[0]["params"]["payload"]["truncated_encoded_public"] == nested["truncated_encoded_public"]
    assert logs[0]["params"]["payload"]["preview"] == {"description": "保留预览"}
    assert logs[0]["params"]["payload"]["diff"] == [
        {"field": "chapters.style", "before": "古典", "after": "清新"},
    ]
    assert logs[0]["messages"][1]["content"] == "作者正文"
    assert json.loads(logs[0]["error"]) == {"message": "正常错误"}
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task_id)).checkpoint["_analysis_completion"] == sentinel
        assert await db.get(AgentConversation, conversation_id) is not None


async def test_backup_model_metadata_requires_trusted_owner_provenance(app, client, make_client):
    owner = await register(client, "backup-provenance-owner")
    other_client = make_client()
    other = await register(other_client, "backup-provenance-other")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        rows = [
            GenerationLog(project_id=project["id"], actor_user_id=owner["id"], kind="generate", status="success",
                          model_source="actor", model_config_owner_id=owner["id"], provider="author-provider",
                          model="author-model", params={"provider": "author-provider", "model": "author-model"}),
            GenerationLog(project_id=project["id"], actor_user_id=owner["id"], kind="generate", status="success",
                          provider="legacy-provider", model="legacy-model", params={"model": "legacy-model"}),
            GenerationLog(project_id=project["id"], actor_user_id=owner["id"], kind="generate", status="error",
                          model_source="actor", model_config_owner_id=other["id"], provider="PRIVATE_PROVIDER",
                          model="PRIVATE_MODEL", error="PRIVATE_MODEL_ERROR",
                          params={"_model_source": "actor", "_model_config_owner_id": owner["id"],
                                  "nested": {"provider-name": "PRIVATE_PROVIDER", "model.name": "PRIVATE_MODEL"}},
                          messages=[{"role": "tool", "content": json.dumps({"model-name": "PRIVATE_MODEL"})}]),
        ]
        db.add_all(rows)
        await db.commit()
        ids = [row.id for row in rows]
    response = await client.get(f"/api/v1/projects/{project['id']}/backup", params={"logs": "true"})
    assert response.status_code == 200, response.text
    records = {row["id"]: row for row in response.json()["tables"]["generation_logs"]}
    assert (records[ids[0]]["provider"], records[ids[0]]["model"]) == ("author-provider", "author-model")
    assert (records[ids[1]]["provider"], records[ids[1]]["model"]) == ("legacy-provider", "legacy-model")
    assert records[ids[2]]["provider"] == records[ids[2]]["model"] == ""
    assert "PRIVATE_PROVIDER" not in response.text and "PRIVATE_MODEL" not in response.text
    assert all("model_source" not in row and "model_config_owner_id" not in row for row in records.values())


async def test_private_root_fields_do_not_make_backup_unimportable(app, client):
    owner = await register(client, "backup-private-root")
    project = await create_project(client)
    secret = "ROOT-PRIVATE-IDENTITY"
    payload = {"field": "tasks.task_key", "before": secret, "after": secret}
    async with app.state.sessionmaker() as db:
        db.add(GenerationLog(project_id=project["id"], actor_user_id=owner["id"], kind="generate", status="error",
                             params=payload, messages=[payload], error=json.dumps(payload)))
        await db.commit()
    backup_response = await client.get(f"/api/v1/projects/{project['id']}/backup", params={"logs": "true"})
    assert backup_response.status_code == 200, backup_response.text
    backup = backup_response.json()
    log = backup["tables"]["generation_logs"][0]
    assert log["params"] == {} and log["messages"] == [] and log["error"] == ""
    assert secret not in backup_response.text
    raw = json.dumps(backup, ensure_ascii=False).encode()
    imported = await client.post("/api/v1/projects/import", files={"file": ("backup.json", raw, "application/json")})
    assert imported.status_code == 201, imported.text


@pytest.mark.parametrize("version", [8, exporter.VERSION])
async def test_imported_backup_cannot_forge_model_provenance(app, client, make_client, version):
    owner = await register(client, "backup-source-owner")
    receiver = make_client()
    recipient = await register(receiver, "backup-source-recipient")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        db.add(GenerationLog(project_id=project["id"], actor_user_id=owner["id"], kind="generate", status="success",
                             provider="可迁移提供方", model="可迁移模型", params={"prompt": "保留"}))
        await db.commit()
    response = await client.get(f"/api/v1/projects/{project['id']}/backup", params={"logs": "true"})
    assert response.status_code == 200, response.text
    backup = response.json()
    backup["version"] = version
    log = backup["tables"]["generation_logs"][0]
    log["model_source"] = "actor"
    log["model_config_owner_id"] = recipient["id"]
    log["params"]["_model_source"] = "actor"
    log["params"]["_model_config_owner_id"] = recipient["id"]
    raw = json.dumps(backup, ensure_ascii=False).encode()
    imported = await receiver.post("/api/v1/projects/import", files={"file": ("backup.json", raw, "application/json")})
    assert imported.status_code == 201, imported.text
    async with app.state.sessionmaker() as db:
        saved = await db.scalar(select(GenerationLog).where(
            GenerationLog.project_id == imported.json()["project"]["id"],
        ))
        assert saved is not None and saved.actor_user_id == recipient["id"]
        assert saved.model_source is None and saved.model_config_owner_id is None
        assert (saved.provider, saved.model) == ("可迁移提供方", "可迁移模型")
