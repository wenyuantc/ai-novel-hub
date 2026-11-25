"""旧版运行中被强制终止后，从冷库快照复验遗留任务。"""

from __future__ import annotations

import argparse
import asyncio
import errno
import hashlib
import json
import os
import secrets
import signal
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import c8_drill as c8
import httpx
from cryptography.fernet import Fernet


async def verify_port_closed(base: str) -> None:
    port = urlsplit(base).port
    assert port is not None
    async with asyncio.timeout(5):
        while True:
            try:
                _reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 1)
            except OSError as exc:
                if exc.errno == errno.ECONNREFUSED:
                    return
                raise
            except TimeoutError:
                pass
            else:
                writer.close()
                await writer.wait_closed()
            await asyncio.sleep(0.1)


async def task_and_chapter_states(url: str, data: dict) -> tuple[dict[str, str], dict[str, str]]:
    task_ids = {
        key: int(data[key]["id"])
        for key in ("analysis_running", "analysis_queued", "generating_task", "queued_generation", "kb_queued")
    }
    task_ids.update(paused_batch=int(data["paused_batch"]["id"]), paused_agent=int(data["agent_task"]["id"]))
    chapter_ids = {key: int(data[key]["id"]) for key in ("generating_chapter", "queued_chapter")}
    rows = await c8.query(url, f"SELECT id,status FROM tasks WHERE id IN ({','.join(map(str, task_ids.values()))})")
    task_rows = dict(rows)
    rows = await c8.query(
        url, f"SELECT id,status FROM chapters WHERE id IN ({','.join(map(str, chapter_ids.values()))})"
    )
    chapter_rows = dict(rows)
    return (
        {key: task_rows[ident] for key, ident in task_ids.items()},
        {key: chapter_rows[ident] for key, ident in chapter_ids.items()},
    )


async def exercise(shape: str, root: Path, pg: dict | None) -> None:
    versions = await c8.prepare(root)
    old, new = versions["old"], versions["new"]
    url = c8.url_for(root, shape, pg, "c8base")
    key, jwt = Fernet.generate_key().decode(), secrets.token_hex(32)
    old_env = c8.app_env(root, old, url, key, jwt)
    await c8.migrate(old, old_env)
    assert await c8.version(url) == "0043"
    model = c8.Model()
    async with c8.fake_model(model) as model_url:
        data = await c8.seed_old(root, old, old_env, model, model_url)
        initial = await c8.query(url, f"SELECT status FROM tasks WHERE id={int(data['interrupted_task']['id'])}")
        assert initial == [("interrupted",)]
        c8.mark("优雅停止对照", old_generation="interrupted")
        controller_pid, controller_group = os.getpid(), os.getpgrp()
        async with c8.application(root, old, old_env, "old-crash") as (base, process), AsyncExitStack() as stack:
            assert os.getpgid(process.pid) == process.pid != controller_group
            author, member, admin = [await c8.login(base, name) for name in (c8.AUTHOR, c8.MEMBER, c8.ADMIN)]
            for client in (author, member, admin):
                stack.push_async_callback(client.aclose)
            p, q = data["project"]["id"], data["member_project"]["id"]
            first = await c8.chapter(author, p, "异常退出分析一", "虚构已保存正文一。")
            second = await c8.chapter(author, p, "异常退出分析二", "虚构已保存正文二。")
            data["generating_chapter"] = await c8.chapter(member, q, "异常退出运行章节")
            data["queued_chapter"] = await c8.chapter(member, q, "异常退出排队章节")
            analysis_call = model.expect(gated=True, text=c8.ANALYSIS)
            data["analysis_running"] = c8.checked(await author.post(f"{c8.API}/chapters/{first['id']}/analyze"), 202)
            await asyncio.wait_for(analysis_call.entered.wait(), 40)
            data["analysis_queued"] = c8.checked(await author.post(f"{c8.API}/chapters/{second['id']}/analyze"), 202)
            await c8.task_state(author, data["analysis_queued"]["id"], {"queued"})
            data["generating_task"], generation_call = await c8.generate(
                member, model, data["generating_chapter"]["id"], gated=True
            )
            await c8.task_state(member, data["generating_task"]["id"], {"running"})
            data["queued_generation"] = c8.checked(
                await member.post(f"{c8.API}/chapters/{data['queued_chapter']['id']}/generate", json={}), 202
            )
            await c8.task_state(member, data["queued_generation"]["id"], {"queued"})
            queued = c8.checked(
                await author.post(
                    f"{c8.API}/projects/{data['knowledge_project']['id']}/kb/documents",
                    files={"file": ("crash.md", b"# C8 fixture\n\nfictional document", "text/markdown")},
                ),
                202,
            )
            data["kb_queued"], data["kb_queued_document"] = queued["task"], queued["document"]
            await c8.task_state(author, data["kb_queued"]["id"], {"queued"})
            tasks_before, chapters_before = await task_and_chapter_states(url, data)
            assert tasks_before == {
                "analysis_running": "running",
                "analysis_queued": "queued",
                "generating_task": "running",
                "queued_generation": "queued",
                "kb_queued": "queued",
                "paused_batch": "paused",
                "paused_agent": "paused",
            }
            assert set(chapters_before.values()) == {"generating"}
            assert analysis_call.entered.is_set() and generation_call.entered.is_set()
            assert not analysis_call.release.is_set() and not generation_call.release.is_set()
            assert process.returncode is None
            c8.mark(
                "强制终止前旧库状态",
                pid=process.pid,
                task_ids={
                    key: int(data[key]["id"])
                    for key in (
                        "analysis_running",
                        "analysis_queued",
                        "generating_task",
                        "queued_generation",
                        "kb_queued",
                    )
                },
                tasks=tasks_before,
                chapters=chapters_before,
                blocked_model_calls=2,
            )
            process.kill()
            exit_code = await asyncio.wait_for(process.wait(), 15)
            assert exit_code == -signal.SIGKILL
            assert not analysis_call.release.is_set() and not generation_call.release.is_set()
            await verify_port_closed(base)
            assert os.getpid() == controller_pid and os.getpgrp() == controller_group
            probe = model.expect()
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                response = await client.post(
                    model_url + "/v1/chat/completions", headers={"Authorization": "Bearer " + c8.MODEL_KEY}, json={}
                )
            assert response.status_code == 200 and probe.entered.is_set() and probe.closed.is_set()
            assert not analysis_call.release.is_set() and not generation_call.release.is_set()
            c8.mark(
                "旧进程异常终止",
                pid=process.pid,
                signal="SIGKILL",
                exit_code=exit_code,
                gates_still_blocked=True,
                app_port_closed=True,
                controller_alive=True,
                model_http=response.status_code,
            )
        # 进程已退出后才放开假模型屏障，避免测试服务持有遗留连接。
        analysis_call.release.set()
        generation_call.release.set()
        await asyncio.wait_for(asyncio.gather(analysis_call.closed.wait(), generation_call.closed.wait()), 15)
        if pg is not None:
            async with asyncio.timeout(15):
                while True:
                    other = await c8.query(
                        url,
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND backend_type='client backend' AND pid<>pg_backend_pid()",
                    )
                    if other[0][0] == 0:
                        break
                    await asyncio.sleep(0.1)
        tasks_after, chapters_after = await task_and_chapter_states(url, data)
        assert tasks_after == tasks_before and chapters_after == chapters_before
        c8.mark(
            "停服后持久状态",
            old_process_exited=True,
            postgres_other_clients=0 if pg else None,
            tasks=tasks_after,
            chapters=chapters_after,
        )
        snapshot = await c8.snapshot(root, shape, pg, url, "crash_cold_0043")
        restored = await c8.restore(root, shape, pg, snapshot, "crash_snapshot_verified")
        assert await c8.version(restored) == "0043"
        restored_tasks, restored_chapters = await task_and_chapter_states(restored, data)
        assert restored_tasks == tasks_after and restored_chapters == chapters_after
        c8.mark("异常退出冷快照验证", revision="0043", states_match=True, old_process_exited=True)
        await c8.legacy_scenarios(root, shape, pg, new, key, jwt, model, data, snapshot)
        assert all(model.valid_keys) and model.unexpected == 0 and model.queue.empty()
        c8.mark("异常退出升级场景完成", engine=shape, unexpected_model_calls=model.unexpected)


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
        "异常退出脚本版本",
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
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(1) from None
