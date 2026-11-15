"""模型受理预检：只读选择、原身份与配置实例代次。"""

import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai.providers import create_provider
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from app.models import AIConfig, Project, ProjectCollaborator, Task, User
from app.services import ai_config_service, project_access, project_ai_config
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine.admission import capture_retry_source, consume_task_admission, prepare_task_admission
from tests.helpers import create_ai_config, create_project, register
from tests.test_collaboration_rewrite_identity import _http_clients
from tests.test_collaboration_story_access import _invite


async def _binding(app, actor_id, project_id):
    async with app.state.sessionmaker() as db:
        return await project_ai_config.resolve_project_model(
            db, actor_id, project_id, "text", cipher=app.state.cipher,
        )


async def test_sqlite_actual_api_reuses_id_but_reencrypts_config(app):
    async with _http_clients(app) as (owner, _unused):
        user = await register(owner, "preflight-api-owner")
        project = await create_project(owner)
        old = await create_ai_config(owner)
        binding = await _binding(app, user["id"], project["id"])
        async with app.state.sessionmaker() as db:
            old_ciphertext = (await db.get(AIConfig, old["id"])).api_key_enc
        assert (await owner.delete(f"/api/v1/ai-configs/{old['id']}")).status_code == 204
        replacement = await create_ai_config(owner)
        assert replacement["id"] == old["id"]  # SQLite 默认 rowid 的最高值可以重用。
        async with app.state.sessionmaker() as db:
            current = await db.get(AIConfig, replacement["id"])
            assert current.api_key_enc != old_ciphertext
            with pytest.raises(BadRequestError):
                await project_ai_config.revalidate_project_model(db, binding, cipher=app.state.cipher)


async def test_sqlite_same_id_and_ciphertext_clone_cannot_reauthorize_old_binding(app):
    async with _http_clients(app) as (owner, _unused):
        user = await register(owner, "preflight-clone-owner")
        project = await create_project(owner)
        old = await create_ai_config(owner)
        binding = await _binding(app, user["id"], project["id"])
        async with app.state.sessionmaker() as db:
            row = await db.get(AIConfig, old["id"])
            fields = ("id", "user_id", "name", "provider", "base_url", "api_key_enc", "model", "model_type",
                      "is_default", "thinking_level", "context_window_k", "max_output_k", "created_at", "updated_at")
            captured = {key: getattr(row, key) for key in fields}
        assert (await owner.delete(f"/api/v1/ai-configs/{old['id']}")).status_code == 204
        # 模拟同密文恢复／复制：实例身份必须重新生成，不能由同 ID、字段或冻结时钟冒充。
        async with app.state.sessionmaker() as db:
            replacement = AIConfig(**captured)
            db.add(replacement)
            await db.commit()
        async with app.state.sessionmaker() as db:
            with pytest.raises(BadRequestError):
                await project_ai_config.revalidate_project_model(db, binding, cipher=app.state.cipher)


async def test_preflight_is_read_only_and_dns_does_not_hold_database_lock(app, monkeypatch):
    async with _http_clients(app) as (owner, _unused):
        user = await register(owner, "preflight-read-owner")
        project = await create_project(owner)
        config = await create_ai_config(owner)
        actor = AuthenticatedActor(user["id"], 0)
        statements = []

        def observe(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement.lstrip().split(None, 1)[0].upper())

        event.listen(app.state.engine.sync_engine, "before_cursor_execute", observe)
        try:
            preflight = await project_ai_config.preflight_project_model(
                app.state.sessionmaker, actor, project["id"], "text", cipher=app.state.cipher,
            )
        finally:
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", observe)
        assert statements == ["SELECT"]
        assert preflight.actor == actor and preflight.binding.config_id == config["id"]
        assert "sk-test-abcdefgh1234" not in repr(preflight) and "gpt-test" not in repr(preflight.config)
        with pytest.raises(FrozenInstanceError):
            preflight.config.model = "other"
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_dns(_url):
            entered.set()
            await release.wait()

        monkeypatch.setattr(ai_config_service, "ensure_endpoint_allowed", slow_dns)
        pending = asyncio.create_task(ai_config_service.build_provider(
            preflight.config, app.state.cipher, create_provider, 5, allow_private=False,
        ))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            # DNS 期间另一独立真实 HTTP 事务必须能改变配置。
            changed = await asyncio.wait_for(owner.patch(f"/api/v1/ai-configs/{config['id']}",
                                                         json={"model": "changed"}), 3)
            assert changed.status_code == 200, changed.text
            release.set()
            await asyncio.wait_for(pending, 5)
            async with app.state.sessionmaker() as db:
                db.info["authenticated_actor"] = actor
                with pytest.raises(ConflictError):
                    await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_preflight_original_token_must_remain_valid_at_admission(app):
    async with _http_clients(app) as (owner, _unused):
        user = await register(owner, "preflight-token-owner")
        project = await create_project(owner)
        await create_ai_config(owner)
        actor = AuthenticatedActor(user["id"], 0)
        preflight = await project_ai_config.preflight_project_model(
            app.state.sessionmaker, actor, project["id"], "text", cipher=app.state.cipher,
        )
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == actor.id).values(token_version=User.token_version + 1))
            await db.commit()
        async with app.state.sessionmaker() as db:
            db.info["authenticated_actor"] = actor
            with pytest.raises(UnauthorizedError):
                await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)


async def test_owner_fallback_preflight_must_not_hide_new_personal_default(app):
    async with _http_clients(app) as (owner, editor):
        await register(owner, "preflight-owner")
        editor_user = await register(editor, "preflight-editor")
        project = await create_project(owner)
        await create_ai_config(owner)
        base = f"/api/v1/projects/{project['id']}"
        invite = await owner.post(f"{base}/collaborators", json={
            "username": "preflight-editor", "role": "editor", "allow_owner_ai": True,
        })
        assert invite.status_code == 201, invite.text
        accepted = await editor.post(
            f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
            json={"expected_revision": invite.json()["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
        actor = AuthenticatedActor(editor_user["id"], 0)
        preflight = await project_ai_config.preflight_project_model(
            app.state.sessionmaker, actor, project["id"], "text", cipher=app.state.cipher,
        )
        assert preflight.binding.source == "owner"
        await create_ai_config(editor, name="本人模型")
        async with app.state.sessionmaker() as db:
            db.info["authenticated_actor"] = actor
            with pytest.raises(ConflictError):
                await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)


async def test_model_binding_v2_requires_original_config_instance(app):
    async with _http_clients(app) as (owner, _unused):
        user = await register(owner, "preflight-v2-owner")
        project = await create_project(owner)
        config = await create_ai_config(owner)
        binding = await _binding(app, user["id"], project["id"])
        payload = project_ai_config.serialize_project_model(binding)
        assert payload["version"] == 2
        assert payload["config_instance_key"] == binding.config_instance_key
        expected = {"actor_id": user["id"], "project_id": project["id"], "project_owner_id": user["id"]}
        assert project_ai_config.deserialize_project_model(payload, **expected) == binding
        invalid = [
            {**payload, "version": 1},
            {key: value for key, value in payload.items() if key != "config_instance_key"},
            {**payload, "config_instance_key": str(uuid4()).upper()},
            {**payload, "config_instance_key": "model-private-key"},
        ]
        for data in invalid:
            with pytest.raises(BadRequestError) as error:
                project_ai_config.deserialize_project_model(data, **expected)
            assert "model-private-key" not in str(error.value)
        async with app.state.sessionmaker() as db:
            row = await db.get(AIConfig, config["id"])
            assert row.instance_key == binding.config_instance_key
            with pytest.raises(BadRequestError):
                await project_ai_config.revalidate_project_model(
                    db, replace(binding, config_instance_key=str(uuid4())), cipher=app.state.cipher,
                )
            with pytest.raises(BadRequestError):
                await project_ai_config.revalidate_project_model(
                    db, replace(binding, config_id=True), cipher=app.state.cipher,
                )


async def test_shared_embedding_v2_requires_v2_inner_model(app):
    async with _http_clients(app) as (owner, _unused):
        user = await register(owner, "preflight-vector-owner")
        project = await create_project(owner)
        await create_ai_config(owner, model_type="embedding", name="向量")
        async with app.state.sessionmaker() as db:
            selection = await project_ai_config.resolve_shared_embedding(
                db, user["id"], project["id"], cipher=app.state.cipher,
            )
        payload = project_ai_config.serialize_shared_embedding(selection)
        assert payload["version"] == 2 and payload["model"]["version"] == 2
        expected = {"actor_id": user["id"], "project_id": project["id"], "project_owner_id": user["id"],
                    "task_grant": project_ai_config.TASK_GRANT_ABSENT}
        assert project_ai_config.deserialize_shared_embedding(payload, **expected) == selection
        for value in ({**payload, "version": 1}, {**payload, "model": {**payload["model"], "version": 1}}):
            with pytest.raises(BadRequestError):
                project_ai_config.deserialize_shared_embedding(value, **expected)
        async with app.state.sessionmaker() as db:
            invalid_model = replace(selection.model, config_id=True)
            with pytest.raises(BadRequestError):
                await project_ai_config.revalidate_shared_embedding(
                    db, replace(selection, model=invalid_model), cipher=app.state.cipher,
                )


async def test_preflight_does_not_reveal_database_failure(app, monkeypatch):
    secret = "private SQL parameter and key"

    async def fail(*_args, **_kwargs):
        raise SQLAlchemyError(secret)

    monkeypatch.setattr(project_ai_config, "_read_project_model_preflight", fail)
    with pytest.raises(ConflictError) as error:
        await project_ai_config.preflight_project_model(
            app.state.sessionmaker, AuthenticatedActor(1, 0), 1, "text", cipher=app.state.cipher,
        )
    assert secret not in str(error.value) and error.value.__context__ is None


async def test_forged_preflight_config_cannot_authorize_different_key(app):
    async with _http_clients(app) as (owner, _unused):
        user = await register(owner, "preflight-forgery-owner")
        project = await create_project(owner)
        await create_ai_config(owner)
        actor = AuthenticatedActor(user["id"], 0)
        preflight = await project_ai_config.preflight_project_model(
            app.state.sessionmaker, actor, project["id"], "text", cipher=app.state.cipher,
        )
        forged = replace(preflight, config=replace(preflight.config, api_key_enc=app.state.cipher.encrypt("other")))
        async with app.state.sessionmaker() as db:
            db.info["authenticated_actor"] = actor
            with pytest.raises(BadRequestError):
                await project_ai_config.accept_preflight_project_model(db, forged, cipher=app.state.cipher)


async def test_preflight_cannot_be_forged_for_another_project(app):
    async with _http_clients(app) as (owner, _unused):
        user = await register(owner, "preflight-forged-project-owner")
        first, second = await create_project(owner), await create_project(owner, title="另一作品")
        await create_ai_config(owner)
        actor = AuthenticatedActor(user["id"], 0)
        preflight = await project_ai_config.preflight_project_model(
            app.state.sessionmaker, actor, first["id"], "text", cipher=app.state.cipher,
        )
        assert project_ai_config.require_original_preflight(preflight) is preflight
        # 两本书同属同一作者且使用相同配置，单靠重选字段相等无法证明最初预检的是哪本书。
        forged = replace(preflight, binding=replace(preflight.binding, project_id=second["id"]))
        with pytest.raises(BadRequestError):
            project_ai_config.require_original_preflight(forged)
        # 即使内容完全一致，也不能复制出一个可供 DNS 使用的预检凭据。
        with pytest.raises(BadRequestError):
            project_ai_config.require_original_preflight(replace(preflight))
        async with app.state.sessionmaker() as db:
            db.info["authenticated_actor"] = actor
            with pytest.raises(BadRequestError):
                await project_ai_config.accept_preflight_project_model(db, forged, cipher=app.state.cipher)

        object.__setattr__(preflight.config, "base_url", "https://not-the-original.example/")
        with pytest.raises(BadRequestError):
            project_ai_config.require_original_preflight(preflight)


async def test_preflight_requires_independent_engine_and_same_authorization_runtime(app):
    async with _http_clients(app) as (owner, _unused):
        user = await register(owner, "preflight-session-owner")
        project = await create_project(owner)
        await create_ai_config(owner)
        actor = AuthenticatedActor(user["id"], 0)
        async with app.state.engine.connect() as connection:
            bound_to_connection = async_sessionmaker(connection, expire_on_commit=False)
            with pytest.raises(ConflictError):
                await project_ai_config.preflight_project_model(
                    bound_to_connection, actor, project["id"], "text", cipher=app.state.cipher,
                )
        preflight = await project_ai_config.preflight_project_model(
            app.state.sessionmaker, actor, project["id"], "text", cipher=app.state.cipher,
        )
        foreign_runtime = async_sessionmaker(app.state.engine, info={"authorization_runtime": object()})
        async with foreign_runtime() as db:
            db.info["authenticated_actor"] = actor
            with pytest.raises(BadRequestError):
                await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)


async def test_preflight_permissions_explicit_id_and_invalid_personal_do_not_fallback(app, client, make_client):
    await register(client, "preflight-role-owner")
    editor, viewer, stranger = make_client(), make_client(), make_client()
    editor_user = await register(editor, "preflight-role-editor")
    viewer_user = await register(viewer, "preflight-role-viewer")
    stranger_user = await register(stranger, "preflight-role-stranger")
    project = await create_project(client)
    owner_config = await create_ai_config(client, name="作者私人配置")
    base = f"/api/v1/projects/{project['id']}"
    invite = await client.post(f"{base}/collaborators", json={
        "username": "preflight-role-editor", "role": "editor", "allow_owner_ai": True,
    })
    assert invite.status_code == 201, invite.text
    accepted = await editor.post(
        f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
        json={"expected_revision": invite.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    await _invite(client, viewer, project["id"], "preflight-role-viewer", "viewer")
    actor = AuthenticatedActor(editor_user["id"], 0)
    for user, error in ((viewer_user, ForbiddenError), (stranger_user, NotFoundError)):
        with pytest.raises(error):
            await project_ai_config.preflight_project_model(
                app.state.sessionmaker, AuthenticatedActor(user["id"], 0), project["id"], "text",
                cipher=app.state.cipher,
            )
    with pytest.raises(NotFoundError):
        await project_ai_config.preflight_project_model(
            app.state.sessionmaker, actor, project["id"], "text", cipher=app.state.cipher,
            config_id=owner_config["id"],
        )
    personal = await create_ai_config(editor, name="本人损坏模型")
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(AIConfig.id == personal["id"]).values(api_key_enc="broken-cipher"))
        await db.commit()
    with pytest.raises(BadRequestError) as error:
        await project_ai_config.preflight_project_model(
            app.state.sessionmaker, actor, project["id"], "text", cipher=app.state.cipher,
        )
    assert "作者私人配置" not in str(error.value) and "broken-cipher" not in str(error.value)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Project).where(Project.id == project["id"]).values(deleted_at=datetime.now(UTC)))
        await db.commit()
    # 回收站作品对协作者不可见，不能因为 viewer 身份先返回只读错误而泄漏作品存在性。
    with pytest.raises(NotFoundError):
        await project_ai_config.preflight_project_model(
            app.state.sessionmaker, AuthenticatedActor(viewer_user["id"], 0), project["id"], "text",
            cipher=app.state.cipher,
        )
    async with app.state.sessionmaker() as db:
        await db.execute(update(Project).where(Project.id == project["id"]).values(
            deleted_at=None, deletion_started_at=datetime.now(UTC), deletion_token=str(uuid4()),
        ))
        await db.commit()
    with pytest.raises(ConflictError) as deleting:
        await project_ai_config.preflight_project_model(
            app.state.sessionmaker, AuthenticatedActor(viewer_user["id"], 0), project["id"], "text",
            cipher=app.state.cipher,
        )
    assert deleting.value.extra["code"] == "project_deleting"


async def test_private_endpoint_hint_is_available_only_for_personal_config(app):
    async with _http_clients(app) as (owner, editor):
        owner_user = await register(owner, "preflight-private-owner")
        editor_user = await register(editor, "preflight-private-editor")
        project = await create_project(owner)
        await create_ai_config(owner, base_url="http://169.254.169.254/latest")
        base = f"/api/v1/projects/{project['id']}"
        invitation = await owner.post(f"{base}/collaborators", json={
            "username": "preflight-private-editor", "role": "editor", "allow_owner_ai": True,
        })
        assert invitation.status_code == 201, invitation.text
        accepted = await editor.post(
            f"/api/v1/collaboration/invitations/project/{invitation.json()['invitation_id']}/accept",
            json={"expected_revision": invitation.json()["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
        with pytest.raises(BadRequestError) as own:
            await project_ai_config.preflight_project_model(
                app.state.sessionmaker, AuthenticatedActor(owner_user["id"], 0), project["id"], "text",
                cipher=app.state.cipher,
            )
        assert "ALLOW_PRIVATE_AI_ENDPOINTS" in own.value.detail
        with pytest.raises(BadRequestError) as shared:
            await project_ai_config.preflight_project_model(
                app.state.sessionmaker, AuthenticatedActor(editor_user["id"], 0), project["id"], "text",
                cipher=app.state.cipher,
            )
        assert "169.254.169.254" not in shared.value.detail
        assert "ALLOW_PRIVATE_AI_ENDPOINTS" not in shared.value.detail


async def _retry_world(app, owner, editor):
    await register(owner, "preflight-retry-owner")
    editor_user = await register(editor, "preflight-retry-editor")
    project = await create_project(owner)
    await create_ai_config(owner, name="作者原模型")
    base = f"/api/v1/projects/{project['id']}"
    invite = await owner.post(f"{base}/collaborators", json={
        "username": "preflight-retry-editor", "role": "editor", "allow_owner_ai": True,
    })
    assert invite.status_code == 201, invite.text
    accepted = await editor.post(
        f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
        json={"expected_revision": invite.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    actor = AuthenticatedActor(editor_user["id"], 0)
    chosen = await project_ai_config.preflight_project_model(
        app.state.sessionmaker, actor, project["id"], "text", cipher=app.state.cipher,
    )
    assert chosen.binding.source == "owner"
    grant = chosen.binding.grant_identity
    async with app.state.sessionmaker() as db:
        task = Task(user_id=actor.id, project_id=project["id"], kind="chapter.generate", status="failed", input={
            "request": {"project_id": project["id"]}, "_task_actor_token_version": actor.token_version,
            "_project_model": project_ai_config.serialize_project_model(chosen.binding),
            "_collaboration_grant": {"invitation_id": grant[0], "revision": grant[1]},
        })
        db.add(task)
        await db.commit()
        source = capture_retry_source(task, expected_task_key=task.statistics_key)
    return project, actor, chosen.binding, source


async def test_retry_preflight_preserves_author_fallback_after_personal_default_added(app):
    async with _http_clients(app) as (owner, editor):
        project, actor, original, source = await _retry_world(app, owner, editor)
        await create_ai_config(editor, name="后来新增本人模型")
        statements = []

        def observe(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement.lstrip().split(None, 1)[0].upper())

        event.listen(app.state.engine.sync_engine, "before_cursor_execute", observe)
        try:
            preflight = await project_ai_config.preflight_original_project_model(
                app.state.sessionmaker, actor, source, cipher=app.state.cipher,
            )
        finally:
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", observe)
        assert statements == ["SELECT"]
        assert preflight.binding == original and preflight.binding.source == "owner"
        assert project_ai_config.is_original_model_preflight(preflight)
        assert project_ai_config.require_retry_model_preflight(preflight, source) is preflight
        with pytest.raises(BadRequestError):
            project_ai_config.require_retry_model_preflight(preflight, replace(source))
        with pytest.raises(ConflictError):
            await project_ai_config.preflight_original_project_model(
                app.state.sessionmaker, actor, replace(source), cipher=app.state.cipher,
            )
        async with app.state.sessionmaker() as db:
            db.info["authenticated_actor"] = actor
            # 普通新任务受理不能挪用原任务 fallback；也不能靠伪造空 admission 放行。
            with pytest.raises(BadRequestError):
                await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)
            with pytest.raises(ConflictError):
                await project_ai_config.accept_original_preflight_project_model(
                    db, preflight, admission=None, cipher=app.state.cipher,
                )


@pytest.mark.parametrize("change", ["task_input", "task_uuid", "config_field", "token", "grant", "task_status"])
async def test_retry_preflight_rejects_changed_original_source(app, change):
    async with _http_clients(app) as (owner, editor):
        _project, actor, original, source = await _retry_world(app, owner, editor)
        if change == "task_input":
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, source.task_id)
                task.input = {**task.input, "request": {"project_id": task.project_id, "changed": True}}
                await db.commit()
        elif change == "task_uuid":
            async with app.state.sessionmaker() as db:
                await db.execute(update(Task).where(Task.id == source.task_id).values(statistics_key=str(uuid4())))
                await db.commit()
        elif change == "config_field":
            async with app.state.sessionmaker() as db:
                await db.execute(update(AIConfig).where(AIConfig.id == original.config_id).values(model="新模型"))
                await db.commit()
        elif change == "token":
            async with app.state.sessionmaker() as db:
                await db.execute(update(User).where(User.id == actor.id).values(token_version=User.token_version + 1))
                await db.commit()
        elif change == "grant":
            async with app.state.sessionmaker() as db:
                await db.execute(update(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == original.project_id, ProjectCollaborator.user_id == actor.id,
                ).values(allow_owner_ai=False, revision=ProjectCollaborator.revision + 1))
                await db.commit()
        else:
            async with app.state.sessionmaker() as db:
                await db.execute(update(Task).where(Task.id == source.task_id).values(status="running"))
                await db.commit()
        with pytest.raises((BadRequestError, ConflictError, ForbiddenError, NotFoundError, UnauthorizedError)):
            await project_ai_config.preflight_original_project_model(
                app.state.sessionmaker, actor, source, cipher=app.state.cipher,
            )


async def test_retry_preflight_rejects_same_id_and_ciphertext_replacement(app):
    async with _http_clients(app) as (owner, editor):
        _project, actor, original, source = await _retry_world(app, owner, editor)
        async with app.state.sessionmaker() as db:
            row = await db.get(AIConfig, original.config_id)
            fields = ("id", "user_id", "name", "provider", "base_url", "api_key_enc", "model", "model_type",
                      "is_default", "thinking_level", "context_window_k", "max_output_k", "created_at", "updated_at")
            captured = {key: getattr(row, key) for key in fields}
            await db.delete(row)
            await db.commit()
        async with app.state.sessionmaker() as db:
            db.add(AIConfig(**captured))
            await db.commit()
        with pytest.raises(BadRequestError):
            await project_ai_config.preflight_original_project_model(
                app.state.sessionmaker, actor, source, cipher=app.state.cipher,
            )


async def test_retry_preflight_preserves_personal_config_after_default_changes(app):
    async with _http_clients(app) as (owner, _unused):
        user = await register(owner, "preflight-retry-personal")
        project = await create_project(owner)
        first = await create_ai_config(owner, name="原本人模型")
        actor = AuthenticatedActor(user["id"], 0)
        fresh = await project_ai_config.preflight_project_model(
            app.state.sessionmaker, actor, project["id"], "text", cipher=app.state.cipher,
        )
        async with app.state.sessionmaker() as db:
            task = Task(user_id=actor.id, project_id=project["id"], kind="chapter.generate", status="failed", input={
                "request": {"project_id": project["id"]}, "_task_actor_token_version": actor.token_version,
                "_project_model": project_ai_config.serialize_project_model(fresh.binding),
            })
            db.add(task)
            await db.commit()
            source = capture_retry_source(task, expected_task_key=task.statistics_key)
        second = await create_ai_config(owner, name="后来的默认模型", is_default=True)
        chosen = await project_ai_config.preflight_original_project_model(
            app.state.sessionmaker, actor, source, cipher=app.state.cipher,
        )
        now = await project_ai_config.preflight_project_model(
            app.state.sessionmaker, actor, project["id"], "text", cipher=app.state.cipher,
        )
        assert chosen.binding == fresh.binding and chosen.binding.config_id == first["id"]
        assert chosen.binding.source == "actor" and now.binding.config_id == second["id"]
        assert project_ai_config.require_retry_model_preflight(chosen, source) is chosen


async def test_retry_admission_consumes_exact_original_proof_once(app, monkeypatch):
    async with _http_clients(app) as (owner, editor):
        project, actor, original, source = await _retry_world(app, owner, editor)
        await create_ai_config(editor, name="后来新增本人模型")
        preflight = await project_ai_config.preflight_original_project_model(
            app.state.sessionmaker, actor, source, cipher=app.state.cipher,
        )
        async with app.state.sessionmaker() as db:
            old = await db.get(Task, source.task_id)
            other = Task(user_id=old.user_id, project_id=old.project_id, kind=old.kind, status="failed",
                         input=dict(old.input))
            db.add(other)
            await db.commit()
            other_source = capture_retry_source(other, expected_task_key=other.statistics_key)
        calls = []

        async def fake_build(*_args, **_kwargs):
            calls.append(True)
            return object()

        monkeypatch.setattr(ai_config_service, "build_provider", fake_build)
        ctx = SimpleNamespace(settings=SimpleNamespace(allow_private_ai_endpoints=False, ai_request_timeout=5),
                              cipher=app.state.cipher, factory=None)
        with pytest.raises(ConflictError):
            await prepare_task_admission(ctx, actor, source.kind, {}, model_preflight=preflight,
                                         retry_source=other_source)
        assert calls == []  # 相同配置但不同任务的预检不能先触发 DNS。
        proof = await prepare_task_admission(ctx, actor, source.kind, {}, model_preflight=preflight,
                                             retry_source=source)
        assert calls == [True]
        async with app.state.sessionmaker() as db:
            db.info["authenticated_actor"] = actor
            await project_access.require_project(db, actor.id, project["id"], "ai.use", for_update=True,
                                                 visibility_write=True, grant_identity=original.grant_identity)
            old = await db.get(Task, source.task_id)
            chosen = await consume_task_admission(db, proof, actor=actor, kind=source.kind, params={}, retry_of=old)
            accepted = await project_ai_config.accept_original_preflight_project_model(
                db, chosen, admission=proof, cipher=app.state.cipher,
            )
            assert accepted == original and accepted.source == "owner"
            with pytest.raises(ConflictError):
                await project_ai_config.accept_original_preflight_project_model(
                    db, chosen, admission=proof, cipher=app.state.cipher,
                )
