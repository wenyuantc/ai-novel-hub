"""跨连接验证单项候选写入与真实模型 HTTP 发送的 PostgreSQL 顺序。"""

import asyncio

import pytest
from sqlalchemy import select, text

from app.ai.providers import OpenAICompatProvider
from app.models import Career, Character, Project
from app.schemas.career import CareerUpdate
from app.schemas.story import CharacterCreate
from app.services import ai_dispatch, career_service, story_service
from app.services.ai_dispatch import VisibilityChangedError, guard_provider
from app.services.setting_ai_context import build_scope
from tests.test_ai_visibility_dispatch import _collect, _server, _sse
from tests.test_collaboration_access_postgresql import _setup
from tests.test_collaboration_rewrite_identity import _http_clients
from tests.test_collaboration_single_candidate_visibility import _create

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


@pytest.mark.parametrize("operation", [
    "character_create", "world_create", "career_create", "organization_create",
    "character_alias", "world_alias", "career_name", "organization_name",
])
async def test_candidate_write_waits_for_actual_send_and_rejects_old_scope(
    postgres_collaboration_apps, monkeypatch, operation,
):
    owner_app, guest_app = postgres_collaboration_apps
    kind, action = operation.split("_")
    async with _http_clients(owner_app) as (owner, _unused), _http_clients(guest_app) as (guest, _unused2):
        project, _user, base = await _setup(owner, guest)
        original = await owner.post(f"{base}/characters", json={"name": "青鸿"})
        assert original.status_code == 201, original.text
        path, payload = _create(kind, "青鸿" if action == "create" else "远行")
        if action != "create":
            target = await owner.post(base + path, json=payload)
            assert target.status_code == 201, target.text
            path += f"/{target.json()['id']}"
            payload = {"aliases": ["青鸿"]} if action == "alias" else {"name": "青鸿"}
        async with owner_app.state.sessionmaker() as db:
            old = await build_scope(db, project["id"], texts=("青鸿",))
            assert old.includes("character", original.json()["id"])
        holder_pid = asyncio.get_running_loop().create_future()
        writer_entered = asyncio.Event()
        original_lock = ai_dispatch._lock

        async def track_lock(db, project_id, *, write, include_recycled=False):
            if write:
                writer_entered.set()
            await original_lock(db, project_id, write=write, include_recycled=include_recycled)
            if not write and not holder_pid.done():
                holder_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))

        monkeypatch.setattr(ai_dispatch, "_lock", track_lock)
        monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 3.0)
        async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
            provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), old.stamp,
                                      owner_app.state.sessionmaker)
            sending = asyncio.create_task(
                _collect(provider.generate_stream([{"role": "user", "content": "旧设定"}], "test"))
            )
            writing = None
            try:
                await asyncio.wait_for(received.wait(), 8)
                pid = await asyncio.wait_for(holder_pid, 8)
                async with guest_app.state.sessionmaker() as probe:
                    held = await probe.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid AND locktype='advisory' "
                        "AND mode='ShareLock' AND granted)"
                    ), {"pid": pid})
                    assert held
                method = "POST" if action == "create" else "PATCH"
                writing = asyncio.create_task(guest.request(method, base + path, json=payload))
                await asyncio.wait_for(writer_entered.wait(), 8)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(writing), 0.15)
                headers.set()
                body.set()
                assert await asyncio.wait_for(sending, 8) == ["ok"]
                result = await asyncio.wait_for(writing, 8)
                assert result.status_code == (201 if action == "create" else 200), result.text
                async with owner_app.state.sessionmaker() as db:
                    current = await build_scope(db, project["id"], texts=("青鸿",))
                    assert not current.includes("character", original.json()["id"])
                    assert current.stamp.revision == old.stamp.revision + 1
                    assert current.stamp.epoch == old.stamp.epoch + 1
                with pytest.raises(VisibilityChangedError):
                    await _collect(provider.generate_stream([{"role": "user", "content": "旧设定"}], "test"))
                assert len(requests) == 1
            finally:
                headers.set()
                body.set()
                for task in (sending, writing):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (sending, writing) if task is not None),
                                     return_exceptions=True)


@pytest.mark.parametrize("operation", ["create_character", "rename_career"])
async def test_failed_visibility_revision_cannot_be_committed_later_postgresql(
    postgres_collaboration_apps, monkeypatch, operation,
):
    owner_app, guest_app = postgres_collaboration_apps
    async with _http_clients(owner_app) as (owner, _unused), _http_clients(guest_app) as (guest, _unused2):
        project, user, base = await _setup(owner, guest)
        target = await owner.post(f"{base}/careers", json={"name": "旧职业", "stages": [{"name": "入门"}]})
        assert target.status_code == 201, target.text
        async with owner_app.state.sessionmaker() as db:
            stamp = (await build_scope(db, project["id"])).stamp

        async def fail(*_args, **_kwargs):
            raise RuntimeError("候选代次更新失败")

        if operation == "create_character":
            monkeypatch.setattr(story_service, "_bump_new_candidate_competition", fail)
        else:
            monkeypatch.setattr(story_service.setting_ai_visibility, "bump_visibility_revision", fail)
        async with guest_app.state.sessionmaker() as writer:
            with pytest.raises(RuntimeError, match="候选代次更新失败"):
                if operation == "create_character":
                    await story_service.create_owned(
                        writer, story_service.CHARACTERS, user["id"], project["id"],
                        CharacterCreate(name="不得留存"), manage=True,
                    )
                else:
                    await career_service.update_career(
                        writer, user["id"], project["id"], target.json()["id"],
                        CareerUpdate(name="新职业"), manual=True,
                    )
            await writer.commit()
        async with owner_app.state.sessionmaker() as probe:
            assert await probe.scalar(select(Character.id).where(
                Character.project_id == project["id"], Character.name == "不得留存",
            )) is None
            assert await probe.scalar(select(Career.name).where(Career.id == target.json()["id"])) == "旧职业"
            row = await probe.get(Project, project["id"])
            assert (row.ai_visibility_revision, row.ai_history_epoch) == (stamp.revision, stamp.epoch)


async def test_concurrent_single_creates_detect_cross_kind_competition_postgresql(
    postgres_collaboration_apps, monkeypatch,
):
    owner_app, guest_app = postgres_collaboration_apps
    async with _http_clients(owner_app) as (owner, _unused), _http_clients(guest_app) as (guest, _unused2):
        project, _user, base = await _setup(owner, guest)
        async with owner_app.state.sessionmaker() as db:
            original = (await build_scope(db, project["id"])).stamp
        reached, release = asyncio.Event(), asyncio.Event()
        holder_pid = asyncio.get_running_loop().create_future()
        snapshot = story_service._candidate_snapshot

        async def delayed_snapshot(db, project_id):
            candidates = await snapshot(db, project_id)
            if not reached.is_set():
                holder_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
                reached.set()
                await release.wait()
            return candidates

        monkeypatch.setattr(story_service, "_candidate_snapshot", delayed_snapshot)
        first = asyncio.create_task(owner.post(f"{base}/characters", json={"name": "青鸿"}))
        second = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            pid = await asyncio.wait_for(holder_pid, 8)
            async with guest_app.state.sessionmaker() as probe:
                held = await probe.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid AND locktype='advisory' "
                    "AND mode='ExclusiveLock' AND granted)"
                ), {"pid": pid})
                assert held
            second = asyncio.create_task(guest.post(f"{base}/world", json={"title": "青鸿"}))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(second), 0.15)
            release.set()
            assert (await asyncio.wait_for(first, 8)).status_code == 201
            assert (await asyncio.wait_for(second, 8)).status_code == 201
            async with owner_app.state.sessionmaker() as db:
                current = await build_scope(db, project["id"], texts=("青鸿",))
                assert current.direct == frozenset()
                assert current.stamp.revision == original.revision + 1
                assert current.stamp.epoch == original.epoch + 1
        finally:
            release.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
