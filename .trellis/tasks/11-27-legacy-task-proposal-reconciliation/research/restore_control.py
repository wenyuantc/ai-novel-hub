"""从排空后的真实 0043 快照分别验证升级与回退，两者使用不同目标库。"""

import argparse
import asyncio
import hashlib
import json
import secrets
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from cryptography.fernet import Fernet

TASKS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TASKS / "archive/2026-11/11-20-collab-migration-deploy-drill/research"))
import c8_drill as c8  # noqa: E402 - 复用归档中的隔离演练控制器。

c8.NEW = "ac97d4de084c60c9b50df32143623ae34cc9c546"


def mark_start(shape):
    c8.mark(
        "回退对照启动",
        engine=shape,
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        controller_sha256=hashlib.sha256(Path(c8.__file__).read_bytes()).hexdigest(),
    )


async def run(shape):
    async with AsyncExitStack() as stack:
        root = Path(stack.enter_context(TemporaryDirectory(prefix="legacy34-control-")))
        pg = await stack.enter_async_context(c8.postgres()) if shape == "postgres" else None
        versions = await c8.prepare(root)
        source = c8.url_for(root, shape, pg, "c8base")
        key, jwt = Fernet.generate_key().decode(), secrets.token_hex(32)
        old_env = c8.app_env(root, versions["old"], source, key, jwt)
        await c8.migrate(versions["old"], old_env)
        assert await c8.version(source) == "0043"
        model = c8.Model()
        model_url = await stack.enter_async_context(c8.fake_model(model))
        async with c8.application(root, versions["old"], old_env, "create") as (base, _):
            async with httpx.AsyncClient(base_url=base, timeout=30, trust_env=False) as client:
                await c8.register(client, c8.AUTHOR)
                await c8.configure(client, model_url)
                project = await c8.project(client, "快照内作品")
                pid = project["id"]
                chapter = await c8.chapter(client, pid, "排空对照")
                task, _ = await c8.generate(client, model, chapter["id"])
                original = c8.checked(await client.get(f"{c8.API}/projects/{pid}/chapters/{chapter['id']}"), 200)
                agent = f"{c8.API}/projects/{pid}/agent/conversations"
                conversation = c8.checked(await client.post(agent, json={}), 201)
                detail = f"{agent}/{conversation['id']}"
                model.expect(tool=True)
                sent = c8.checked(await client.post(detail + "/messages", json={"content": "提出虚构角色"}), 202)
                tid = sent["task"]["id"]
                await c8.task_state(client, tid, {"paused"})
                c8.checked(await client.post(f"{c8.API}/tasks/{tid}/cancel"), 200)
                await c8.task_state(client, tid, {"cancelled"})
                changes = c8.checked(await client.get(detail), 200)["changes"]
                assert len(changes) == 1 and changes[0]["status"] == "proposed"
        remaining = await c8.query(source, "SELECT count(*) FROM tasks WHERE status NOT IN ('succeeded','cancelled')")
        assert remaining == [(0,)]
        point = await c8.snapshot(root, shape, pg, source, "drained")
        upgraded = await c8.restore(root, shape, pg, point, "upgrade_control")
        new_env = c8.app_env(root, versions["new"], upgraded, key, jwt)
        await c8.migrate(versions["new"], new_env)
        assert await c8.version(upgraded) == "0079"
        async with c8.application(root, versions["new"], new_env, "upgraded") as (base, _):
            client = await c8.login(base, c8.AUTHOR)
            try:
                marker = await c8.project(client, "升级后独有作品")
            finally:
                await client.aclose()
        recovered = await c8.restore(root, shape, pg, point, "rollback_control")
        assert await c8.version(recovered) == "0043"
        recovered_env = c8.app_env(root, versions["old"], recovered, key, jwt)
        async with c8.application(root, versions["old"], recovered_env, "restored") as (base, _):
            client = await c8.login(base, c8.AUTHOR)
            try:
                state = c8.checked(await client.get(f"{c8.API}/projects/{pid}/chapters/{chapter['id']}"), 200)
                assert all(state[k] == original[k] for k in ("id", "title", "content", "status", "project_id"))
                assert await c8.task_state(client, task["id"], {"succeeded"})
                assert await c8.task_state(client, tid, {"cancelled"})
                assert c8.checked(await client.get(detail), 200)["changes"] == changes
                assert await c8.query(recovered, "SELECT id FROM projects ORDER BY id") == [(pid,)]
                assert await c8.query(upgraded, "SELECT id FROM projects ORDER BY id") == [(pid,), (marker["id"],)]
                extra = await c8.chapter(client, pid, "恢复后验收使用")
                await c8.generate(client, model, extra["id"])
            finally:
                await client.aclose()
        assert model.valid_keys == [True] * 3 and model.unexpected == 0
        c8.mark(
            "回退对照通过",
            engine=shape,
            old_revision="0043",
            upgraded_revision="0079",
            new_only_project=marker["id"],
            restored_chapter_status=state["status"],
            task_statuses=["succeeded", "cancelled"],
            proposal_status=changes[0]["status"],
            restored_public_proposal_sha256=hashlib.sha256(json.dumps(changes, sort_keys=True).encode()).hexdigest(),
            authenticated_model_requests=3,
            unexpected_model_requests=model.unexpected,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("engine", choices=("sqlite", "postgres"))
    engine = parser.parse_args().engine
    mark_start(engine)
    asyncio.run(run(engine))
