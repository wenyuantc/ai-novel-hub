"""单书日志备份不可导出或导入伪造的可信模型来源。"""

import json

import pytest
from sqlalchemy import select

from app.models import GenerationLog
from app.services.project_transfer.registry import RULES_BY_TABLE
from tests.helpers import create_project, register


@pytest.mark.parametrize("spoof", [
    {"model_source": "owner", "model_config_owner_id": 123},
    {"model_source": "actor", "model_config_owner_id": "伪造的非法值"},
    {"model_source": ["owner"], "model_config_owner_id": -1},
])
async def test_project_backup_does_not_export_or_accept_trusted_log_source(app, client, make_client, spoof):
    owner = await register(client, "source-backup-owner")
    importer_client = make_client()
    importer_user = await register(importer_client, "source-backup-importer")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        log = GenerationLog(project_id=project["id"], actor_user_id=owner["id"], kind="generate", status="success",
                            model_source="owner", model_config_owner_id=owner["id"],
                            params={"_model_source": "owner", "_model_config_owner_id": 9876},
                            messages=[{"role": "user", "content": "作者日志"}])
        db.add(log)
        await db.commit()
    response = await client.get(f"/api/v1/projects/{project['id']}/backup", params={"logs": "true"})
    assert response.status_code == 200, response.text
    backup = response.json()
    assert len(backup["tables"]["generation_logs"]) == 1
    row = backup["tables"]["generation_logs"][0]
    assert not {"model_source", "model_config_owner_id", "ai_config_id"} & row.keys()
    assert {"model_source", "model_config_owner_id"} <= set(RULES_BY_TABLE["generation_logs"].exclude)
    assert "_model_source" not in row["params"] and "_model_config_owner_id" not in row["params"]
    # 旧 params 和客户端可任意加字段：只能当不可信数据，不能形成可信来源列。
    row.update(spoof)
    row["params"]["_model_source"] = "owner"
    imported = await importer_client.post("/api/v1/projects/import", files={
        "file": ("spoof.json", json.dumps(backup, ensure_ascii=False).encode(), "application/json"),
    })
    assert imported.status_code == 201, imported.text
    imported_id = imported.json()["project"]["id"]
    async with app.state.sessionmaker() as db:
        imported_log = await db.scalar(select(GenerationLog).where(GenerationLog.project_id == imported_id))
        assert imported_log.actor_user_id == importer_user["id"]
        assert (imported_log.model_source, imported_log.model_config_owner_id) == (None, None)
        assert imported_log.params["_model_source"] == "owner"
    reexport = await importer_client.get(f"/api/v1/projects/{imported_id}/backup", params={"logs": "true"})
    assert reexport.status_code == 200, reexport.text
    assert "_model_source" not in reexport.json()["tables"]["generation_logs"][0]["params"]


async def test_backup_exports_only_owner_log_and_removes_nested_internal_identity(app, client, make_client):
    owner = await register(client, "source-export-owner")
    other_client = make_client()
    other = await register(other_client, "source-export-guest")
    project = await create_project(client)
    payload = {
        "temperature": 0.7,
        "_model_source": "owner",
        "details": [{"model_config_owner_id": other["id"], "kept": "保留"},
                    {"nested": {"_shared_embedding": {"key": "秘密"}, "label": "不变"}}],
        "json_text": json.dumps({"_model_binding": {"owner_id": other["id"]}, "content": "正文"}),
    }
    async with app.state.sessionmaker() as db:
        db.add_all([
            GenerationLog(project_id=project["id"], actor_user_id=owner["id"], kind="generate", status="success",
                          provider="owner-provider", model="owner-model", model_source="actor",
                          model_config_owner_id=owner["id"], params=payload,
                          messages=[{"role": "user", "content": "作者消息"}]),
            GenerationLog(project_id=project["id"], actor_user_id=other["id"], kind="generate", status="success",
                          provider="other-provider-secret", model="other-model-secret", model_source="owner",
                          model_config_owner_id=owner["id"], params={"private": "他人参数"},
                          messages=[{"role": "user", "content": "他人消息"}]),
            GenerationLog(project_id=project["id"], actor_user_id=None, kind="generate", status="success",
                          provider="unknown-provider-secret", model="unknown-model-secret", params={}, messages=[]),
        ])
        await db.commit()
    response = await client.get(f"/api/v1/projects/{project['id']}/backup", params={"logs": "true"})
    assert response.status_code == 200, response.text
    rows = response.json()["tables"]["generation_logs"]
    assert len(rows) == 1 and rows[0]["provider"] == "owner-provider" and rows[0]["model"] == "owner-model"
    assert rows[0]["messages"] == [{"role": "user", "content": "作者消息"}]
    assert rows[0]["params"] == {
        "temperature": 0.7, "details": [{"kept": "保留"}, {"nested": {"label": "不变"}}],
        "json_text": json.dumps({"content": "正文"}, ensure_ascii=False),
    }
    assert "other-provider-secret" not in response.text and "unknown-provider-secret" not in response.text
