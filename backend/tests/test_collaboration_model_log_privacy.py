"""日志模型来源属于受信服务端事实，缺证或作者回退不能暴露作者模型。"""

import json
from uuid import uuid4

import pytest

from app.core.errors import BadRequestError
from app.models import AIConfig, GenerationLog
from app.services import generation_log_service
from app.services.project_ai_config import ResolvedProjectModel
from tests.helpers import create_ai_config, create_project, register
from tests.test_collaboration_log_private_views import _invite


async def test_log_does_not_attach_reused_config_id_to_new_instance(app, client):
    owner = await register(client, "log-reused-model-instance")
    config = await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "章节"})).json()
    async with app.state.sessionmaker() as db:
        original = await db.get(AIConfig, config["id"])
        binding = ResolvedProjectModel(
            project_id=project["id"], actor_id=owner["id"], project_owner_id=owner["id"],
            model_type="text", source="actor", config_id=original.id, config_owner_id=owner["id"],
            grant_identity=None, config_fingerprint="a" * 64, config_instance_key=original.instance_key,
        )
        fields = {name: getattr(original, name) for name in (
            "user_id", "name", "provider", "base_url", "api_key_enc", "model", "model_type",
            "is_default", "thinking_level", "context_window_k", "max_output_k", "created_at", "updated_at",
        )}
        await db.delete(original)
        await db.commit()
    async with app.state.sessionmaker() as db:
        db.add(AIConfig(id=config["id"], instance_key=str(uuid4()), **fields))
        await db.commit()
    async with app.state.sessionmaker() as db:
        log = await generation_log_service.write_log(
            db, project_id=project["id"], actor_user_id=owner["id"], chapter_id=chapter["id"],
            kind="generate", status="success", ai_config_id=config["id"], provider="original-provider",
            model="original-model", params={}, messages=[], model_binding=binding,
        )
        await db.commit()
        log_id = log.id
        assert log.ai_config_id is None
        assert log.model_source == "actor" and log.model_config_owner_id == owner["id"]
    response = await client.get(f"{base}/chapters/{chapter['id']}/generation-logs/{log_id}")
    assert response.status_code == 200, response.text
    assert response.json()["ai_config_id"] is None
    assert response.json()["provider"] == "original-provider" and response.json()["model"] == "original-model"


async def test_old_spoofed_collaborator_log_hides_model_details_and_preserves_storage(app, client, make_client):
    await register(client, "model-log-owner")
    editor = make_client()
    actor = await register(editor, "model-log-editor")
    owner_config = await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "章节"})).json()
    await _invite(client, editor, base, "model-log-editor", "editor")
    async with app.state.sessionmaker() as db:
        log = GenerationLog(project_id=project["id"], chapter_id=chapter["id"], actor_user_id=actor["id"],
                            kind="generate", status="success", ai_config_id=owner_config["id"],
                            provider="PRIVATE_PROVIDER", model="PRIVATE_MODEL", prompt_tokens=37,
                            params={"_model_source": "actor", "_model_config_owner_id": actor["id"],
                                    "nested": json.dumps({"_model_source": "actor", "config_id": owner_config["id"]}),
                                    "aliases": {"provider-name": "SECRET_PROVIDER", "model.name": "SECRET_MODEL",
                                                "nested": json.dumps({"model-name": "SECRET_NESTED"}),
                                                "double": json.dumps(json.dumps({"model.name": "SECRET_DOUBLE"})),
                                                "partial": '{"provider-name":"SECRET_TRUNCATED", "next":'}})
        db.add(log)
        await db.commit()
        log_id = log.id
    response = await editor.get(f"{base}/chapters/{chapter['id']}/generation-logs/{log_id}")
    assert response.status_code == 200, response.text
    public = response.json()
    assert public["ai_config_id"] is None and public["provider"] == "" and public["model"] == ""
    assert public["model_source"] is None and public["prompt_tokens"] == 37
    assert "_model_source" not in response.text and "_model_config_owner_id" not in response.text
    assert json.loads(public["params"]["nested"])["config_id"] is None
    assert public["params"]["aliases"]["provider-name"] is None
    assert public["params"]["aliases"]["model.name"] is None
    assert json.loads(public["params"]["aliases"]["nested"])["model-name"] is None
    assert json.loads(json.loads(public["params"]["aliases"]["double"]))["model.name"] is None
    assert "SECRET_" not in response.text
    async with app.state.sessionmaker() as db:
        row = await db.get(GenerationLog, log_id)
        assert row.ai_config_id == owner_config["id"] and row.provider == "PRIVATE_PROVIDER"
        assert row.model == "PRIVATE_MODEL" and row.prompt_tokens == 37


async def test_bound_actor_and_owner_models_keep_privacy_after_config_deletion(app, client, make_client):
    owner = await register(client, "model-binding-owner")
    editor = make_client()
    actor = await register(editor, "model-binding-editor")
    owner_config = await create_ai_config(client)
    actor_config = await create_ai_config(editor)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "章节"})).json()
    member = await _invite(client, editor, base, "model-binding-editor", "editor")
    grant = (member["invitation_id"], member["revision"])
    async with app.state.sessionmaker() as db:
        rows = []
        for source, config, config_owner, provider, model in (
            ("actor", actor_config, actor, "SELF_PROVIDER", "SELF_MODEL"),
            ("owner", owner_config, owner, "OWNER_PROVIDER", "OWNER_MODEL"),
        ):
            binding = ResolvedProjectModel(project_id=project["id"], actor_id=actor["id"],
                                           project_owner_id=owner["id"], model_type="text", source=source,
                                           config_id=config["id"], config_owner_id=config_owner["id"],
                                           grant_identity=grant, config_fingerprint="a" * 64,
                                           config_instance_key=(await db.get(AIConfig, config["id"])).instance_key)
            log = await generation_log_service.write_log(
                db, project_id=project["id"], actor_user_id=actor["id"], chapter_id=chapter["id"],
                kind="generate", status="success", ai_config_id=config["id"], provider=provider, model=model,
                params={"_model_source": "forged", "_model_config_owner_id": -1,
                        "nested": {"provider": provider, "model": model, "api_key": "never-disclose",
                                   "APIKey": "never-disclose-either"}},
                messages=[{"role": "tool", "content": json.dumps({
                    "provider": provider, "model": model, "api_key": "never-disclose",
                })}], model_binding=binding,
            )
            log.prompt_tokens = 41
            rows.append(log)
        await db.commit()
        ids = [row.id for row in rows]
        await db.delete(await db.get(AIConfig, owner_config["id"]))
        await db.delete(await db.get(AIConfig, actor_config["id"]))
        await db.commit()

    endpoint = f"{base}/chapters/{chapter['id']}/generation-logs"
    own = (await editor.get(f"{endpoint}/{ids[0]}")).json()
    fallback = (await editor.get(f"{endpoint}/{ids[1]}")).json()
    assert own["model_source"] == "actor" and own["ai_config_id"] is None
    assert (own["provider"], own["model"]) == ("SELF_PROVIDER", "SELF_MODEL")
    assert "never-disclose" not in str(own) and "api_key" not in own["params"]["nested"]
    assert fallback["model_source"] == "owner" and fallback["ai_config_id"] is None
    assert fallback["provider"] == fallback["model"] == ""
    assert fallback["params"]["nested"] == {"provider": None, "model": None}
    assert json.loads(fallback["messages"][0]["content"]) == {"provider": None, "model": None}
    assert own["prompt_tokens"] == fallback["prompt_tokens"] == 41
    async with app.state.sessionmaker() as db:
        row = await db.get(GenerationLog, ids[1])
        assert row.model_source == "owner" and row.model_config_owner_id == owner["id"]
        assert (row.provider, row.model) == ("OWNER_PROVIDER", "OWNER_MODEL")


async def test_untrusted_params_cannot_create_model_source_proof(app, client):
    actor = await register(client, "model-binding-spoof")
    config = await create_ai_config(client)
    project = await create_project(client)
    fake_params = {"_model_source": "actor", "_model_config_owner_id": actor["id"],
                   "_collaboration_grant": {"invitation_id": "forged", "revision": 1}}
    async with app.state.sessionmaker() as db:
        row = await generation_log_service.write_log(
            db, project_id=project["id"], actor_user_id=actor["id"], chapter_id=None, kind="generate",
            status="success", ai_config_id=config["id"], provider="private", model="private-model",
            params=fake_params, messages=[],
        )
        await db.commit()
        assert row.model_source is None and row.model_config_owner_id is None and row.params == {}
        assert fake_params["_model_source"] == "actor"
        forged_binding = ResolvedProjectModel(project_id=project["id"], actor_id=actor["id"] + 1,
                                              project_owner_id=actor["id"], model_type="text", source="actor",
                                              config_id=config["id"], config_owner_id=actor["id"] + 1,
                                              grant_identity=None, config_fingerprint="a" * 64,
                                              config_instance_key=(await db.get(AIConfig, config["id"])).instance_key)
        with pytest.raises(BadRequestError):
            await generation_log_service.write_log(
                db, project_id=project["id"], actor_user_id=actor["id"], chapter_id=None, kind="generate",
                status="success", ai_config_id=config["id"], provider="private", model="private-model",
                params={}, messages=[], model_binding=forged_binding,
            )


async def test_legacy_author_model_display_survives_configuration_deletion(app, client):
    owner = await register(client, "legacy-model-author")
    config = await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "章节"})).json()
    async with app.state.sessionmaker() as db:
        log = GenerationLog(project_id=project["id"], chapter_id=chapter["id"], actor_user_id=owner["id"],
                            kind="generate", status="success", ai_config_id=config["id"],
                            provider="legacy-provider", model="legacy-model", prompt_tokens=19)
        db.add(log)
        await db.commit()
        log_id = log.id
        await db.delete(await db.get(AIConfig, config["id"]))
        await db.commit()
    response = await client.get(f"{base}/chapters/{chapter['id']}/generation-logs/{log_id}")
    assert response.status_code == 200, response.text
    assert response.json()["model_source"] is None and response.json()["ai_config_id"] is None
    assert response.json()["provider"] == "legacy-provider" and response.json()["model"] == "legacy-model"
    assert response.json()["prompt_tokens"] == 19
