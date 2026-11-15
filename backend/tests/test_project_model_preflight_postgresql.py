"""只读模型预检与锁后受理在 PostgreSQL 两连接下的真实顺序。"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, update

from app.core.errors import BadRequestError, ConflictError, UnauthorizedError
from app.models import AIConfig, Project, Task, User
from app.services import ai_config_service, project_access, project_ai_config
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine.admission import capture_retry_source, consume_task_admission, prepare_task_admission
from tests.helpers import create_ai_config
from tests.test_collaboration_access_postgresql import _setup, _wait_for_blocker
from tests.test_collaboration_rewrite_identity import _http_clients

pytest_plugins = ("tests.test_collaboration_ai_config_postgresql",)


async def _world(owner_app, owner, guest):
    project, editor, base = await _setup(owner, guest)
    config = await create_ai_config(owner)
    allowed = await owner.patch(f"{base}/collaborators/{editor['id']}", json={
        "expected_revision": 2, "allow_owner_ai": True,
    })
    assert allowed.status_code == 200, allowed.text
    async with owner_app.state.sessionmaker() as db:
        owner_id = await db.scalar(select(Project.user_id).where(Project.id == project["id"]))
    return project, AuthenticatedActor(editor["id"], 0), owner_id, config


async def test_open_preflight_read_holds_no_config_or_user_write_lock(postgres_collaboration_apps, monkeypatch):
    owner_app, guest_app = postgres_collaboration_apps
    async with _http_clients(owner_app) as (owner, _unused), _http_clients(guest_app) as (guest, _unused2):
        project, actor, _owner_id, _config = await _world(owner_app, owner, guest)
        reached, release = asyncio.Event(), asyncio.Event()
        holder_pid = asyncio.get_running_loop().create_future()
        original = project_ai_config._read_project_model_preflight

        async def pause_after_read(db, *args, **kwargs):
            result = await original(db, *args, **kwargs)
            holder_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            reached.set()
            await release.wait()
            return result

        monkeypatch.setattr(project_ai_config, "_read_project_model_preflight", pause_after_read)
        reading = asyncio.create_task(project_ai_config.preflight_project_model(
            guest_app.state.sessionmaker, actor, project["id"], "text", cipher=guest_app.state.cipher,
        ))
        try:
            await asyncio.wait_for(reached.wait(), 8)
            pid = await asyncio.wait_for(holder_pid, 8)
            async with owner_app.state.sessionmaker() as probe:
                has_advisory = await probe.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid AND locktype='advisory' AND granted)"
                ), {"pid": pid})
                assert not has_advisory
            # 预检事务仍停在 SELECT 后，编辑者本人配置写入应可在其完成前提交。
            await asyncio.wait_for(create_ai_config(guest, name="并发个人模型"), 5)
            assert not reading.done()
            release.set()
            preflight = await asyncio.wait_for(reading, 8)
            assert preflight.binding.source == "owner"
            async with guest_app.state.sessionmaker() as db:
                db.info["authenticated_actor"] = actor
                with pytest.raises(ConflictError):
                    await project_ai_config.accept_preflight_project_model(db, preflight, cipher=guest_app.state.cipher)
        finally:
            release.set()
            if not reading.done():
                reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)


async def test_final_admission_waits_then_rejects_changed_config(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with _http_clients(owner_app) as (owner, _unused), _http_clients(guest_app) as (_guest, _unused2):
        project, actor, owner_id, config = await _world(owner_app, owner, _guest)
        preflight = await project_ai_config.preflight_project_model(
            guest_app.state.sessionmaker, actor, project["id"], "text", cipher=guest_app.state.cipher,
        )
        pending = None
        try:
            async with owner_app.state.sessionmaker() as writer:
                await ai_config_service.lock_config_owner(writer, owner_id, write=True)
                await writer.execute(update(AIConfig).where(AIConfig.id == config["id"]).values(model="并发变化"))
                pid = await writer.scalar(text("SELECT pg_backend_pid()"))

                async def accept():
                    async with guest_app.state.sessionmaker() as db:
                        db.info["authenticated_actor"] = actor
                        return await project_ai_config.accept_preflight_project_model(
                            db, preflight, cipher=guest_app.state.cipher,
                        )

                pending = asyncio.create_task(accept())
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 8)
                await writer.commit()
            with pytest.raises(ConflictError):
                await asyncio.wait_for(pending, 8)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)


async def test_postgresql_same_id_ciphertext_and_timestamp_clone_rejects_old_binding(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with _http_clients(owner_app) as (owner, _unused), _http_clients(guest_app) as (guest, _unused2):
        project, actor, _owner_id, config = await _world(owner_app, owner, guest)
        preflight = await project_ai_config.preflight_project_model(
            guest_app.state.sessionmaker, actor, project["id"], "text", cipher=guest_app.state.cipher,
        )
        async with owner_app.state.sessionmaker() as db:
            row = await db.get(AIConfig, config["id"])
            fields = ("id", "user_id", "name", "provider", "base_url", "api_key_enc", "model", "model_type",
                      "is_default", "thinking_level", "context_window_k", "max_output_k", "created_at", "updated_at")
            captured = {key: getattr(row, key) for key in fields}
            original_key = row.instance_key
        assert (await owner.delete(f"/api/v1/ai-configs/{config['id']}")).status_code == 204
        async with owner_app.state.sessionmaker() as db:
            replacement = AIConfig(**captured)
            db.add(replacement)
            await db.commit()
            assert replacement.instance_key != original_key
        async with guest_app.state.sessionmaker() as db:
            with pytest.raises(BadRequestError):
                await project_ai_config.revalidate_project_model(db, preflight.binding, cipher=guest_app.state.cipher)
        async with guest_app.state.sessionmaker() as db:
            db.info["authenticated_actor"] = actor
            with pytest.raises(ConflictError):
                await project_ai_config.accept_preflight_project_model(db, preflight, cipher=guest_app.state.cipher)


async def test_postgresql_token_revocation_after_preflight_rejects_admission(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with _http_clients(owner_app) as (owner, _unused), _http_clients(guest_app) as (guest, _unused2):
        project, actor, _owner_id, _config = await _world(owner_app, owner, guest)
        preflight = await project_ai_config.preflight_project_model(
            guest_app.state.sessionmaker, actor, project["id"], "text", cipher=guest_app.state.cipher,
        )
        async with owner_app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == actor.id).values(token_version=User.token_version + 1))
            await db.commit()
        async with guest_app.state.sessionmaker() as db:
            db.info["authenticated_actor"] = actor
            with pytest.raises(UnauthorizedError):
                await project_ai_config.accept_preflight_project_model(db, preflight, cipher=guest_app.state.cipher)


async def test_postgresql_retry_original_waits_for_config_then_rejects_change(postgres_collaboration_apps, monkeypatch):
    owner_app, guest_app = postgres_collaboration_apps
    async with _http_clients(owner_app) as (owner, _unused), _http_clients(guest_app) as (guest, _unused2):
        project, actor, owner_id, config = await _world(owner_app, owner, guest)
        fresh = await project_ai_config.preflight_project_model(
            guest_app.state.sessionmaker, actor, project["id"], "text", cipher=guest_app.state.cipher,
        )
        original = fresh.binding
        async with guest_app.state.sessionmaker() as db:
            task = Task(user_id=actor.id, project_id=project["id"], kind="chapter.generate", status="failed", input={
                "request": {"project_id": project["id"]}, "_task_actor_token_version": actor.token_version,
                "_project_model": project_ai_config.serialize_project_model(original),
                "_collaboration_grant": {"invitation_id": original.grant_identity[0],
                                         "revision": original.grant_identity[1]},
            })
            db.add(task)
            await db.commit()
            source = capture_retry_source(task, expected_task_key=task.statistics_key)
        await create_ai_config(guest, name="后来新增本人默认")
        preflight = await project_ai_config.preflight_original_project_model(
            guest_app.state.sessionmaker, actor, source, cipher=guest_app.state.cipher,
        )
        assert preflight.binding == original and original.source == "owner"

        async def fake_build(*_args, **_kwargs):
            return object()

        monkeypatch.setattr(ai_config_service, "build_provider", fake_build)
        ctx = SimpleNamespace(settings=SimpleNamespace(allow_private_ai_endpoints=False, ai_request_timeout=5),
                              cipher=guest_app.state.cipher, factory=None)
        proof = await prepare_task_admission(ctx, actor, source.kind, {}, model_preflight=preflight,
                                             retry_source=source)
        pending = None
        try:
            async with owner_app.state.sessionmaker() as writer:
                await ai_config_service.lock_config_owner(writer, owner_id, write=True)
                await writer.execute(update(AIConfig).where(AIConfig.id == config["id"]).values(model="并发变化"))
                pid = await writer.scalar(text("SELECT pg_backend_pid()"))

                async def accept():
                    async with guest_app.state.sessionmaker() as db:
                        db.info["authenticated_actor"] = actor
                        await project_access.require_project(
                            db, actor.id, project["id"], "ai.use", for_update=True, visibility_write=True,
                            grant_identity=original.grant_identity,
                        )
                        old = await db.get(Task, source.task_id)
                        selected = await consume_task_admission(
                            db, proof, actor=actor, kind=source.kind, params={}, retry_of=old,
                        )
                        return await project_ai_config.accept_original_preflight_project_model(
                            db, selected, admission=proof, cipher=guest_app.state.cipher,
                        )

                pending = asyncio.create_task(accept())
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 8)
                await writer.commit()
            with pytest.raises(ConflictError):
                await asyncio.wait_for(pending, 8)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
