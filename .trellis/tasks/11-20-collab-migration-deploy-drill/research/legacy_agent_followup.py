"""补验旧版待审核智能体任务的继续、同会话重发和审核操作。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import c8_drill as c8
from cryptography.fernet import Fernet


async def exercise(shape: str, root: Path, pg: dict | None) -> None:
    versions = await c8.prepare(root)
    old, new = versions["old"], versions["new"]
    source = c8.url_for(root, shape, pg, "c8base")
    key, jwt = Fernet.generate_key().decode(), secrets.token_hex(32)
    old_env = c8.app_env(root, old, source, key, jwt)
    await c8.migrate(old, old_env)
    model = c8.Model()
    async with c8.fake_model(model) as model_url:
        data = await c8.seed_old(root, old, old_env, model, model_url)
        snapshot = await c8.snapshot(root, shape, pg, source, "agent_paused")
        for case in ("message_paused", "resume", "finish", "reject", "cancel_and_send"):
            url = await c8.restore(root, shape, pg, snapshot, "agent_" + case)
            env = c8.app_env(root, new, url, key, jwt)
            await c8.migrate(new, env)
            async with c8.application(root, new, env, "agent-" + case) as (base, _):
                author = await c8.login(base, c8.AUTHOR)
                try:
                    task = (await c8.tasks_by_id(author))[data["agent_task"]["id"]]
                    assert task["status"] == "paused"
                    agent_base = f"{c8.API}/projects/{data['project']['id']}/agent"
                    conversation_url = agent_base + f"/conversations/{data['conversation']['id']}"
                    detail = c8.checked(await author.get(conversation_url), 200)
                    proposals = [row for row in detail["changes"] if row["status"] == "proposed"]
                    assert len(proposals) == 1
                    probe = model.expect(gated=True)
                    result = {"case": case, "before": task["status"], "proposal_before": proposals[0]["status"]}
                    if case == "cancel_and_send":
                        cancelled = await author.post(
                            f"{c8.API}/tasks/{task['id']}/cancel", params={"task_key": task["task_key"]}
                        )
                        await asyncio.sleep(2)
                        result["cancel_http"] = cancelled.status_code
                        result["after_cancel"] = (await c8.tasks_by_id(author))[task["id"]]["status"]
                    if case in {"message_paused", "cancel_and_send"}:
                        response = await author.post(conversation_url + "/messages", json={"content": "虚构的新一轮"})
                    elif case in {"resume", "finish"}:
                        response = await author.post(
                            f"{c8.API}/tasks/{task['id']}/{case}", params={"task_key": task["task_key"]}
                        )
                    else:
                        body = {
                            "task_key": task["task_key"],
                            "changes": [
                                {"id": row["id"], "change_key": row["change_key"], "view_version": row["view_version"]}
                                for row in proposals
                            ],
                        }
                        response = await author.post(agent_base + f"/turns/{task['id']}/reject-all", json=body)
                    await asyncio.sleep(8 if 200 <= response.status_code < 300 else 2)
                    result["http"] = response.status_code
                    result["old_task_after"] = (await c8.tasks_by_id(author))[task["id"]]["status"]
                    result["model_called"] = probe.entered.is_set()
                    if probe.entered.is_set():
                        probe.release.set()
                        await asyncio.wait_for(probe.closed.wait(), 15)
                        await asyncio.sleep(1)
                    else:
                        assert model.queue.get_nowait() is probe
                    if response.status_code == 202 and case in {"message_paused", "cancel_and_send"}:
                        new_task = response.json()["task"]
                        result["new_task_after"] = (await c8.tasks_by_id(author))[new_task["id"]]["status"]
                    detail = c8.checked(await author.get(conversation_url), 200)
                    result["proposal_after"] = [row["status"] for row in detail["changes"]]
                    c8.mark("旧智能体任务补验", **result)
                finally:
                    await author.aclose()
        assert all(model.valid_keys) and model.unexpected == 0 and model.queue.empty()
        c8.mark("旧智能体任务补验完成", engine=shape, cases=5, unexpected_model_calls=model.unexpected)


async def main(shape: str, root: Path) -> None:
    if shape == "sqlite":
        await exercise(shape, root, None)
    else:
        async with c8.postgres() as pg:
            await exercise(shape, root, pg)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shape", choices=("sqlite", "postgres"), required=True)
    args = parser.parse_args()
    c8.mark(
        "智能体补验脚本版本",
        sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        controller_sha256=hashlib.sha256(Path(c8.__file__).read_bytes()).hexdigest(),
        engine=args.shape,
    )
    with TemporaryDirectory(prefix="ainovel-c8-drill-") as temporary:
        try:
            asyncio.run(main(args.shape, Path(temporary).resolve()))
        except BaseException as exc:
            import traceback

            frames = traceback.extract_tb(exc.__traceback__)
            frame = frames[-1] if frames else None
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "type": type(exc).__name__,
                        "file": Path(frame.filename).name if frame else "",
                        "line": frame.lineno if frame else 0,
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(1) from None
