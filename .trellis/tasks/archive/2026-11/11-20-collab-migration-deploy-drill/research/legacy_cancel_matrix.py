"""C8：在独立克隆中补验旧任务的本人／管理员取消与重新发起。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import c8_drill as c8
from cryptography.fernet import Fernet


async def reissue(client, model, data, kind):
    if kind == "analysis":
        chapter = await c8.chapter(client, data["project"]["id"], "C8 分析重发", "虚构正文。")
        probe = model.expect(gated=True, text=c8.ANALYSIS)
        response = await client.post(f"{c8.API}/chapters/{chapter['id']}/analyze")
    else:
        probe = model.expect(gated=True)
        ident = data["batch_second"]["id"]
        response = await client.post(
            f"{c8.API}/projects/{data['project']['id']}/batch-generate",
            json={
                "start_chapter_id": ident,
                "end_chapter_id": ident,
                "auto_continue": False,
            },
        )
    result = {"http": response.status_code}
    if response.status_code == 202:
        task = response.json()
        await asyncio.sleep(8)
        result.update(
            status_after_8s=(await c8.tasks_by_id(client))[task["id"]]["status"], model_called=probe.entered.is_set()
        )
    if probe.entered.is_set():
        probe.release.set()
        await asyncio.wait_for(probe.closed.wait(), 15)
        if response.status_code == 202:
            final = await c8.task_state(
                client, task["id"], {"succeeded", "failed", "cancelled"}, task_key=task.get("task_key")
            )
            result["final_status"] = final["status"]
    else:
        assert model.queue.get_nowait() is probe
    return result


async def exercise(shape: str, root: Path, pg: dict | None) -> None:
    versions = await c8.prepare(root)
    old, new = versions["old"], versions["new"]
    url = c8.url_for(root, shape, pg, "c8base")
    key, jwt = Fernet.generate_key().decode(), secrets.token_hex(32)
    old_env = c8.app_env(root, old, url, key, jwt)
    await c8.migrate(old, old_env)
    model = c8.Model()
    async with c8.fake_model(model) as model_url:
        data = await c8.seed_old(root, old, old_env, model, model_url)
        undrained, _ = await c8.active_old_snapshot(root, shape, pg, url, old, old_env, model, data)
        cases = (
            ("running_analysis_owner", "analysis_running", "owner"),
            ("running_analysis_admin", "analysis_running", "admin"),
            ("queued_analysis_admin", "analysis_queued", "admin"),
            ("paused_batch_owner", "paused_batch", "owner"),
            ("paused_batch_admin", "paused_batch", "admin"),
            ("paused_agent_owner", "agent_task", "owner"),
            ("paused_agent_admin", "agent_task", "admin"),
            ("interrupted_chapter_owner", "generating_task", "owner"),
            ("interrupted_chapter_admin", "generating_task", "admin"),
        )
        for label, source, actor in cases:
            case_url = await c8.restore(root, shape, pg, undrained, "matrix_" + label)
            env = c8.app_env(root, new, case_url, key, jwt)
            await c8.migrate(new, env)
            async with c8.application(root, new, env, "matrix-" + label) as (base, _), AsyncExitStack() as stack:
                author, member, admin = [await c8.login(base, name) for name in (c8.AUTHOR, c8.MEMBER, c8.ADMIN)]
                for client in (author, member, admin):
                    stack.push_async_callback(client.aclose)
                await asyncio.sleep(2)
                owner = member if source == "generating_task" else author
                task = (await c8.tasks_by_id(owner))[data[source]["id"]]
                before = task["status"]
                if actor == "admin":
                    response = await admin.post(
                        f"{c8.API}/admin/tasks/{task['task_key']}/cancel",
                        json={"confirm": True, "reason": "C8 取消矩阵演练"},
                    )
                    duration = 12 if response.status_code == 202 else 2
                else:
                    response = await owner.post(
                        f"{c8.API}/tasks/{task['id']}/cancel", params={"task_key": task["task_key"]}
                    )
                    duration = 2
                await asyncio.sleep(duration)
                after = (await c8.tasks_by_id(owner))[task["id"]]["status"]
                result = {
                    "case": label,
                    "before": before,
                    "http": response.status_code,
                    "after": after,
                    "observation_seconds": duration,
                }
                if source == "generating_task":
                    chapter_id = data["generating_chapter"]["id"]
                    states = await c8.query(case_url, f"SELECT status FROM chapters WHERE id={chapter_id}")
                    result["chapter_status"] = states[0][0]
                    retried = await member.post(f"{c8.API}/chapters/{chapter_id}/generate", json={})
                    result["reissue_http"] = retried.status_code
                if label == "queued_analysis_admin" and after == "cancelled":
                    result["reissue"] = await reissue(author, model, data, "analysis")
                if label == "paused_batch_owner" and after == "cancelled":
                    result["reissue"] = await reissue(author, model, data, "batch")
                c8.mark("旧任务取消补验", **result)
        assert all(model.valid_keys) and model.unexpected == 0 and model.queue.empty()
        c8.mark("旧任务取消补验完成", engine=shape, cases=len(cases), unexpected_model_calls=model.unexpected)


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
        "补验脚本版本",
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
