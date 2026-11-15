"""日志和模型实例的服务端身份不得因整数 ID 复用或单书导入而被补认。"""

import json
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from app.core.errors import ConflictError
from app.models import AgentChange, GenerationLog
from app.services.agent import effects, service
from tests.helpers import create_project, register


async def test_agent_effect_does_not_identify_reused_log_by_actor_and_same_time(app, client):
    owner = await register(client, "log-identity-effect-owner")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "章节"})).json()
    conversation = (await client.post(f"{base}/agent/conversations", json={})).json()
    async with app.state.sessionmaker() as db:
        original = GenerationLog(project_id=project["id"], actor_user_id=owner["id"],
                                 kind="generate", status="success")
        db.add(original)
        await db.flush()
        before = await effects.snapshot(db, project["id"])
        original.chapter_id = chapter["id"]
        await db.flush()
        captured = effects.diff(before, await effects.snapshot(db, project["id"]))
        assert any(item["table"] == "generation_logs" for item in captured)
        change = AgentChange(conversation_id=conversation["id"], project_id=project["id"],
                             tool_call_id="log-effect", tool_name="test", kind="small", status="executed",
                             arguments={}, preview={}, effects=captured)
        db.add(change)
        await db.commit()
        log_id, created_at = original.id, original.created_at
    endpoint = f"{base}/agent/conversations/{conversation['id']}"
    first = await client.get(endpoint)
    assert first.status_code == 200, first.text
    assert any(item["table"] == "generation_logs" for item in first.json()["changes"][0]["effects"])
    async with app.state.sessionmaker() as db:
        await db.delete(await db.get(GenerationLog, log_id))
        await db.commit()
    async with app.state.sessionmaker() as db:
        db.add(GenerationLog(id=log_id, project_id=project["id"], actor_user_id=owner["id"],
                             chapter_id=chapter["id"], kind="generate", status="success", created_at=created_at))
        await db.commit()
    second = await client.get(endpoint)
    assert second.status_code == 200, second.text
    assert all(item["table"] != "generation_logs" for item in second.json()["changes"][0]["effects"])


async def test_single_book_backup_cannot_reuse_log_or_model_instance_identity(app, client, make_client):
    owner = await register(client, "log-identity-exporter")
    importer = make_client()
    imported_owner = await register(importer, "log-identity-importer")
    project = await create_project(client)
    model_key = str(uuid4())
    async with app.state.sessionmaker() as db:
        row = GenerationLog(project_id=project["id"], actor_user_id=owner["id"], kind="generate", status="success",
                            model_source="actor", model_config_owner_id=owner["id"],
                            model_config_instance_key=model_key)
        db.add(row)
        await db.commit()
        old_key = row.log_instance_key
        assert str(UUID(old_key)) == old_key and UUID(old_key).version == 4
    backup_response = await client.get(f"/api/v1/projects/{project['id']}/backup", params={"logs": "true"})
    assert backup_response.status_code == 200, backup_response.text
    backup = backup_response.json()
    record = backup["tables"]["generation_logs"][0]
    assert "log_instance_key" not in record and "model_config_instance_key" not in record
    record.update(log_instance_key=old_key, model_config_instance_key=model_key,
                  model_source="actor", model_config_owner_id=imported_owner["id"])
    response = await importer.post("/api/v1/projects/import", files={
        "file": ("forged.json", json.dumps(backup, ensure_ascii=False).encode(), "application/json"),
    })
    assert response.status_code == 201, response.text
    async with app.state.sessionmaker() as db:
        imported = await db.scalar(select(GenerationLog).where(
            GenerationLog.project_id == response.json()["project"]["id"],
        ))
        assert imported.log_instance_key != old_key and UUID(imported.log_instance_key).version == 4
        assert imported.model_config_instance_key is None
        assert imported.model_source is None and imported.model_config_owner_id is None


async def test_legacy_created_at_proof_cannot_claim_keyed_log(app, client):
    """旧格式（仅 created_at）证明只对无实例键的旧行有效；对有键的行投影隐藏、撤销冲突。"""
    owner = await register(client, "log-identity-legacy-proof")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "章节"})).json()
    conversation = (await client.post(f"{base}/agent/conversations", json={})).json()
    async with app.state.sessionmaker() as db:
        log = GenerationLog(project_id=project["id"], actor_user_id=owner["id"], chapter_id=chapter["id"],
                            kind="generate", status="success")
        db.add(log)
        await db.commit()
        before = await effects.snapshot(db, project["id"])
        log.chapter_id = None
        after = await effects.snapshot(db, project["id"])
        await db.commit()
        keyed = next(item for item in effects.diff(before, after) if item["table"] == "generation_logs")
        assert keyed["_private_identity"] == {"actor_user_id": owner["id"], "log_instance_key": log.log_instance_key}
        legacy = deepcopy(keyed)
        legacy["_private_identity"] = {"actor_user_id": owner["id"], "created_at": jsonable_encoder(log.created_at)}
        change = AgentChange(conversation_id=conversation["id"], project_id=project["id"],
                             tool_call_id="legacy-proof", tool_name="test", kind="small", status="executed",
                             arguments={}, preview={}, effects=[legacy])
        db.add(change)
        await db.commit()
        projected = (await service.public_changes(db, owner["id"], project["id"], [change]))[0]
        assert all(item["table"] != "generation_logs" for item in projected.effects)
        with pytest.raises(ConflictError):
            await effects.undo(db, project["id"], [legacy])
        await db.rollback()
        await db.refresh(log)
        assert log.chapter_id is None
        # 正控制：同一效果带真实实例键证明时可以撤销。
        await effects.undo(db, project["id"], [keyed])
        await db.commit()
        await db.refresh(log)
        assert log.chapter_id == chapter["id"]


@pytest.mark.parametrize("side", ["before", "after"])
async def test_link_only_effect_cannot_insert_or_delete_log(app, client, side):
    """diff 只为两侧都存在的生成记录产生关联效果；缺一侧的旧或伪造效果撤销时须冲突，不删除也不插入记录。"""
    owner = await register(client, f"log-identity-link-{side}")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "章节"})).json()
    async with app.state.sessionmaker() as db:
        log = GenerationLog(project_id=project["id"], actor_user_id=owner["id"], chapter_id=chapter["id"],
                            kind="generate", status="success")
        db.add(log)
        await db.commit()
        before = await effects.snapshot(db, project["id"])
        log.chapter_id = None
        keyed = next(item for item in effects.diff(before, await effects.snapshot(db, project["id"]))
                     if item["table"] == "generation_logs")
        await db.commit()
        forged = deepcopy(keyed)
        if side == "before":
            # 撤销会删除当前记录。
            forged["before"] = None
            forged["after"] = {**forged["after"], "chapter_id": None}
        else:
            # 撤销会以缺少实例键的值插入一条“原记录”。
            await db.delete(log)
            await db.commit()
            forged["after"] = None
        with pytest.raises(ConflictError):
            await effects.undo(db, project["id"], [forged])
        await db.rollback()
        rows = list(await db.scalars(select(GenerationLog).where(GenerationLog.project_id == project["id"])))
        assert len(rows) == (1 if side == "before" else 0)
