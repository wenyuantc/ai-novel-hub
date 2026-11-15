"""真实 PostgreSQL 中验证模型原绑定、撤权和配置变更的事务屏障。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text, update

from app.core.errors import BadRequestError, ForbiddenError, NotFoundError
from app.models import AIConfig, Project, ProjectCollaborator, User
from app.services import (
    ai_config_service,
    project_ai_config,
    project_lifecycle,
)
from tests import pg_support
from tests.helpers import create_ai_config, register
from tests.test_collaboration_access_postgresql import _setup, _wait_for_blocker
from tests.test_collaboration_rowlock_postgresql import _assert_blocked_on_project_row


@pytest.fixture
async def postgres_collaboration_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url("AINOVEL_COLLAB_MODEL_POSTGRES_TEST_URL", "collab_models", purpose="模型测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2,
    ) as apps:
        yield apps


async def _prepare(owner_app, owner, guest):
    project, editor, base = await _setup(owner, guest)
    config = await create_ai_config(owner)
    response = await owner.patch(f"{base}/collaborators/{editor['id']}", json={
        "expected_revision": 2, "allow_owner_ai": True,
    })
    assert response.status_code == 200, response.text
    async with owner_app.state.sessionmaker() as db:
        binding = await project_ai_config.resolve_project_model(
            db, editor["id"], project["id"], "text", cipher=owner_app.state.cipher,
        )
        owner_id = await db.scalar(select(Project.user_id).where(Project.id == project["id"]))
        binding = project_ai_config.deserialize_project_model(
            project_ai_config.serialize_project_model(binding), actor_id=editor["id"], project_id=project["id"],
            project_owner_id=owner_id,
        )
    assert binding.source == "owner" and binding.config_id == config["id"]
    return project, editor, base, binding


@pytest.mark.parametrize("state", ["revoked", "viewer"])
async def test_revalidate_waits_for_project_then_sees_revocation(postgres_collaboration_apps, state):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, editor, _, binding = await _prepare(owner_app, owner, guest)
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                await project_lifecycle.lock_active(holder, project["id"])
                member = await holder.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == editor["id"],
                ))
                if state == "revoked":
                    member.status = state
                else:
                    member.role = state
                member.revision += 1
                await holder.flush()
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))

                async def verify():
                    async with guest_app.state.sessionmaker() as db:
                        await project_ai_config.revalidate_project_model(
                            db, binding, cipher=guest_app.state.cipher, for_update=True, lock_config=True,
                        )

                pending = asyncio.create_task(verify())
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                await _assert_blocked_on_project_row(guest_app.state.sessionmaker, pid)
                await holder.commit()
            with pytest.raises((ForbiddenError, NotFoundError)):
                await asyncio.wait_for(pending, 6)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_config_shared_lock_serializes_key_changes(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        _, _, _, binding = await _prepare(owner_app, owner, guest)
        pending = None
        try:
            async with guest_app.state.sessionmaker() as send:
                await project_ai_config.revalidate_project_model(
                    send, binding, cipher=guest_app.state.cipher, lock_config=True,
                )
                pid = await send.scalar(text("SELECT pg_backend_pid()"))

                async def replace_key():
                    async with owner_app.state.sessionmaker() as db:
                        await db.execute(update(AIConfig).where(AIConfig.id == binding.config_id).values(
                            api_key_enc=owner_app.state.cipher.encrypt("changed-key"),
                        ))
                        await db.commit()

                pending = asyncio.create_task(replace_key())
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, pid), 6)
                assert not pending.done()
                await send.rollback()
            await asyncio.wait_for(pending, 6)
            async with guest_app.state.sessionmaker() as db:
                with pytest.raises(BadRequestError, match="模型配置已失效"):
                    await project_ai_config.revalidate_project_model(
                        db, binding, cipher=guest_app.state.cipher, lock_config=True,
                    )
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_config_deleted_before_lock_cannot_switch(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        _, _, _, binding = await _prepare(owner_app, owner, guest)
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                cfg = await holder.get(AIConfig, binding.config_id)
                await holder.delete(cfg)
                await holder.flush()
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))

                async def verify():
                    async with guest_app.state.sessionmaker() as db:
                        await project_ai_config.revalidate_project_model(db, binding, cipher=guest_app.state.cipher)

                pending = asyncio.create_task(verify())
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                await holder.commit()
            with pytest.raises(BadRequestError, match="模型配置已失效"):
                await asyncio.wait_for(pending, 6)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_revalidation_lock_precedes_revocation(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        _, editor, base, binding = await _prepare(owner_app, owner, guest)
        pending = None
        try:
            async with guest_app.state.sessionmaker() as holder:
                await project_ai_config.revalidate_project_model(
                    holder, binding, cipher=guest_app.state.cipher, for_update=True,
                )
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                pending = asyncio.create_task(owner.delete(f"{base}/collaborators/{editor['id']}", params={
                    "expected_revision": 3,
                }))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, pid), 6)
                assert not pending.done()
                await holder.rollback()
            response = await asyncio.wait_for(pending, 6)
            assert response.status_code == 204, response.text
            async with guest_app.state.sessionmaker() as db:
                with pytest.raises(NotFoundError):
                    await project_ai_config.revalidate_project_model(db, binding, cipher=guest_app.state.cipher)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_shared_vector_guard_blocks_new_latest_without_default(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, editor, _, _ = await _prepare(owner_app, owner, guest)
        first = await create_ai_config(owner, name="first-vector", model_type="embedding")
        async with owner_app.state.sessionmaker() as db:
            await db.execute(update(AIConfig).where(AIConfig.id == first["id"]).values(is_default=False))
            await db.commit()
        async with guest_app.state.sessionmaker() as db:
            selected = await project_ai_config.resolve_shared_embedding(
                db, editor["id"], project["id"], cipher=guest_app.state.cipher,
            )
        assert selected.model.config_id == first["id"]
        pending = None
        try:
            async with guest_app.state.sessionmaker() as holder:
                await project_ai_config.revalidate_shared_embedding(holder, selected, cipher=guest_app.state.cipher)
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                # 无默认时，新建非默认 B 不会更新 A，但 B 的更大 ID 会改变最新选择。
                pending = asyncio.create_task(create_ai_config(
                    owner, name="second-vector", model_type="embedding", is_default=False,
                ))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, pid), 6)
                assert not pending.done()
                await holder.rollback()
            second = await asyncio.wait_for(pending, 6)
            assert second["id"] != first["id"] and second["is_default"] is False
            async with guest_app.state.sessionmaker() as db:
                with pytest.raises(BadRequestError, match="项目向量模型已变化"):
                    await project_ai_config.revalidate_shared_embedding(db, selected, cipher=guest_app.state.cipher)
                new = await project_ai_config.resolve_shared_embedding(
                    db, editor["id"], project["id"], cipher=guest_app.state.cipher,
                )
                assert new.model.config_id == second["id"]
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


@pytest.mark.parametrize("action", ["set_default", "delete"])
async def test_shared_vector_guard_blocks_owner_config_mutations(postgres_collaboration_apps, action):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, editor, _, _ = await _prepare(owner_app, owner, guest)
        first = await create_ai_config(owner, name="old-vector", model_type="embedding")
        second = await create_ai_config(owner, name="new-vector", model_type="embedding")
        async with guest_app.state.sessionmaker() as db:
            selected = await project_ai_config.resolve_shared_embedding(
                db, editor["id"], project["id"], cipher=guest_app.state.cipher,
            )
        assert selected.model.config_id == first["id"]
        pending = None
        try:
            async with guest_app.state.sessionmaker() as holder:
                await project_ai_config.revalidate_shared_embedding(holder, selected, cipher=guest_app.state.cipher)
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                if action == "set_default":
                    call = owner.post(f"/api/v1/ai-configs/{second['id']}/default")
                else:
                    call = owner.delete(f"/api/v1/ai-configs/{first['id']}")
                pending = asyncio.create_task(call)
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, pid), 6)
                assert not pending.done()
                await holder.rollback()
            response = await asyncio.wait_for(pending, 6)
            assert response.status_code == (200 if action == "set_default" else 204), response.text
            async with guest_app.state.sessionmaker() as db:
                with pytest.raises(BadRequestError):
                    await project_ai_config.revalidate_shared_embedding(db, selected, cipher=guest_app.state.cipher)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


@pytest.mark.parametrize("action", ["set_default", "delete", "new_latest"])
async def test_shared_vector_guard_sees_config_change_committed_first(postgres_collaboration_apps, action):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, editor, _, _ = await _prepare(owner_app, owner, guest)
        first = await create_ai_config(owner, name="old-vector", model_type="embedding")
        second = await create_ai_config(owner, name="new-vector", model_type="embedding")
        if action == "new_latest":
            async with owner_app.state.sessionmaker() as db:
                await db.execute(update(AIConfig).where(AIConfig.id == first["id"]).values(is_default=False))
                await db.commit()
        async with guest_app.state.sessionmaker() as db:
            selected = await project_ai_config.resolve_shared_embedding(
                db, editor["id"], project["id"], cipher=guest_app.state.cipher,
            )
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                # 模拟配置 CRUD 内获得用户独占锁后修改，但暂不提交。
                await ai_config_service.lock_config_owner(holder, selected.project_owner_id, write=True)
                if action == "set_default":
                    await holder.execute(update(AIConfig).where(AIConfig.id == first["id"]).values(is_default=False))
                    await holder.execute(update(AIConfig).where(AIConfig.id == second["id"]).values(is_default=True))
                elif action == "delete":
                    cfg = await holder.get(AIConfig, first["id"])
                    await holder.delete(cfg)
                    await holder.flush()
                else:
                    holder.add(AIConfig(
                        user_id=selected.project_owner_id, name="latest-vector", provider="openai_compat",
                        model="latest", model_type="embedding", is_default=False,
                        api_key_enc=owner_app.state.cipher.encrypt("later"),
                    ))
                    await holder.flush()
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))

                async def verify():
                    async with guest_app.state.sessionmaker() as db:
                        await project_ai_config.revalidate_shared_embedding(db, selected, cipher=guest_app.state.cipher)

                pending = asyncio.create_task(verify())
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                await holder.commit()
            with pytest.raises(BadRequestError):
                await asyncio.wait_for(pending, 6)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_shared_vector_guard_does_not_block_another_users_config(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, editor, _, _ = await _prepare(owner_app, owner, guest)
        await create_ai_config(owner, model_type="embedding")
        async with guest_app.state.sessionmaker() as db:
            selected = await project_ai_config.resolve_shared_embedding(
                db, editor["id"], project["id"], cipher=guest_app.state.cipher,
            )
        async with guest_app.state.sessionmaker() as holder:
            await project_ai_config.revalidate_shared_embedding(holder, selected, cipher=guest_app.state.cipher)
            # 不同用户配置增删应继续运行，证明不是全表门禁。
            other = await asyncio.wait_for(create_ai_config(guest, model_type="embedding"), 6)
            assert other["id"] != selected.model.config_id
            await holder.rollback()


@pytest.mark.parametrize("change", ["revoke", "disable"])
async def test_config_http_write_waits_for_auth_change_then_rejects(postgres_collaboration_apps, change):
    """真实 User 行锁先提交时，配置写入必须按请求捕获的令牌版本失败。"""
    owner_app, _ = postgres_collaboration_apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver",
    ) as owner:
        user = await register(owner, "config-auth-pg-owner")
        cfg = await create_ai_config(owner, model="original")
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                values = {"token_version": User.token_version + 1} if change == "revoke" else {"is_active": False}
                await holder.execute(update(User).where(User.id == user["id"]).values(**values))
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                pending = asyncio.create_task(owner.patch(
                    f"/api/v1/ai-configs/{cfg['id']}", json={"model": "must-not-commit"},
                ))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, pid), 10)
                assert not pending.done()
                await holder.commit()
            response = await asyncio.wait_for(pending, 10)
            assert response.status_code == 401, response.text
            async with owner_app.state.sessionmaker() as db:
                assert await db.scalar(select(AIConfig.model).where(AIConfig.id == cfg["id"])) == "original"
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_config_http_write_holds_actor_lock_while_waiting_for_config(postgres_collaboration_apps, monkeypatch):
    """配置锁等待期间，撤令牌不能越过本次已取得的 User 屏障。"""
    owner_app, _ = postgres_collaboration_apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver",
    ) as owner:
        user = await register(owner, "config-lock-pg-owner")
        await create_ai_config(owner, name="existing")
        pending = revocation = None
        entered = asyncio.Event()
        writer_pid = None
        original = ai_config_service.lock_config_owner

        async def observe_writer(db, user_id, *, write):
            nonlocal writer_pid
            if user_id == user["id"] and write:
                writer_pid = await db.scalar(text("SELECT pg_backend_pid()"))
                entered.set()
            await original(db, user_id, write=write)

        try:
            async with owner_app.state.sessionmaker() as holder:
                await original(holder, user["id"], write=False)
                holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                monkeypatch.setattr(ai_config_service, "lock_config_owner", observe_writer)
                pending = asyncio.create_task(owner.post("/api/v1/ai-configs", json={
                    "name": "并发新配置", "provider": "openai_compat", "model": "m",
                }))
                await asyncio.wait_for(entered.wait(), 10)
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder_pid), 10)

                async def revoke():
                    async with owner_app.state.sessionmaker() as db:
                        await db.execute(update(User).where(User.id == user["id"]).values(
                            token_version=User.token_version + 1,
                        ))
                        await db.commit()

                revocation = asyncio.create_task(revoke())
                assert writer_pid is not None
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, writer_pid), 10)
                assert not pending.done() and not revocation.done()
                await holder.rollback()
            response = await asyncio.wait_for(pending, 10)
            await asyncio.wait_for(revocation, 10)
            assert response.status_code == 201, response.text
            async with owner_app.state.sessionmaker() as db:
                assert await db.scalar(select(AIConfig.id).where(
                    AIConfig.user_id == user["id"], AIConfig.name == "并发新配置",
                )) is not None
                assert await db.scalar(select(User.token_version).where(User.id == user["id"])) == 1
        finally:
            for task in (pending, revocation):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


@pytest.mark.parametrize("for_update", [False, True])
async def test_model_selection_holds_both_users_config_snapshot(postgres_collaboration_apps, monkeypatch, for_update):
    """两用户的配置不能在本人缺失与作者回退之间跨快照插入。"""
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, editor, base = await _setup(owner, guest)
        changed = await owner.patch(f"{base}/collaborators/{editor['id']}", json={
            "expected_revision": 2, "allow_owner_ai": True,
        })
        assert changed.status_code == 200, changed.text
        entered, release = asyncio.Event(), asyncio.Event()
        holder_pid = None
        original = project_ai_config._default_config

        async def read_then_wait(db, user_id, model_type):
            nonlocal holder_pid
            row = await original(db, user_id, model_type)
            if user_id == editor["id"] and model_type == "text":
                holder_pid = await db.scalar(text("SELECT pg_backend_pid()"))
                entered.set()
                await release.wait()
            return row

        monkeypatch.setattr(project_ai_config, "_default_config", read_then_wait)

        async def resolve():
            async with guest_app.state.sessionmaker() as db:
                return await project_ai_config.resolve_project_model(
                    db, editor["id"], project["id"], "text", cipher=guest_app.state.cipher,
                    for_update=for_update,
                )

        resolution = asyncio.create_task(resolve())
        writers = []
        try:
            await asyncio.wait_for(entered.wait(), 10)
            writers = [
                asyncio.create_task(create_ai_config(guest, name="actor-now")),
                asyncio.create_task(create_ai_config(owner, name="owner-now")),
            ]
            assert holder_pid is not None
            query = text("SELECT count(*) FROM pg_stat_activity "
                         "WHERE datname=current_database() AND :holder=ANY(pg_blocking_pids(pid))")

            async def both_wait():
                while True:
                    async with guest_app.state.sessionmaker() as db:
                        if await db.scalar(query, {"holder": holder_pid}) >= 2:
                            return
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(both_wait(), 10)
            assert not any(writer.done() for writer in writers)
            release.set()
            with pytest.raises(BadRequestError, match="尚未配置文本模型"):
                await asyncio.wait_for(resolution, 10)
            personal, _author = await asyncio.wait_for(asyncio.gather(*writers), 10)
            fresh = await resolve()
            assert fresh.source == "actor" and fresh.config_id == personal["id"]
        finally:
            release.set()
            for task in [resolution, *writers]:
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


async def test_model_selection_rechecks_membership_after_waiting_for_config(postgres_collaboration_apps):
    """非写事务在等待配置锁后必须重新读取邀请，不能使用此前的 editor 快照。"""
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, editor, base = await _setup(owner, guest)
        config = await create_ai_config(guest)
        async with owner_app.state.sessionmaker() as db:
            owner_id = await db.scalar(select(Project.user_id).where(Project.id == project["id"]))
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                await ai_config_service.lock_config_owner(holder, owner_id, write=True)
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))

                async def resolve():
                    async with guest_app.state.sessionmaker() as db:
                        return await project_ai_config.resolve_project_model(
                            db, editor["id"], project["id"], "text", cipher=guest_app.state.cipher,
                            config_id=config["id"],
                        )

                pending = asyncio.create_task(resolve())
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 10)
                revoked = await owner.delete(f"{base}/collaborators/{editor['id']}", params={"expected_revision": 2})
                assert revoked.status_code == 204, revoked.text
                await holder.rollback()
            with pytest.raises(NotFoundError):
                await asyncio.wait_for(pending, 10)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
