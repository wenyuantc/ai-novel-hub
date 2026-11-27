"""TODO 34: exercise retained 0043 agent proposals on isolated database copies."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import secrets
import sqlite3
import sys
from contextlib import aclosing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx
from cryptography.fernet import Fernet

TASKS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TASKS / "archive/2026-11/11-20-collab-migration-deploy-drill/research"))
c8 = importlib.import_module("c8_drill")

TARGET = "ac97d4de084c60c9b50df32143623ae34cc9c546"


def digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


async def changes(url):
    if url.startswith("sqlite"):
        columns = [row[1] for row in await c8.query(url, "PRAGMA table_info(agent_changes)")]
    else:
        columns = [
            row[0]
            for row in await c8.query(
                url,
                """
            SELECT column_name FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'agent_changes' ORDER BY ordinal_position
        """,
            )
        ]
    rows = await c8.query(url, "SELECT * FROM agent_changes ORDER BY id")
    assert len(rows) == 1
    return dict(zip(columns, rows[0], strict=True))


async def compare(url, baseline, case, stage, expected_tasks, model, expected_calls, change_key=None):
    row = await changes(url)
    projected = {key: row[key] for key in baseline}
    assert projected == baseline, [key for key in baseline if projected[key] != baseline[key]]
    extra = {key: row[key] for key in row.keys() - baseline.keys()}
    version = await c8.version(url)
    if version == "0043":
        assert not extra
    else:
        assert version == "0079"
        assert set(extra) == {"change_key", "turn_task_key", "external_worker_key"}
        assert UUID(extra["change_key"]).version == 4
        assert extra["turn_task_key"] is None and extra["external_worker_key"] is None
        if change_key is not None:
            assert extra["change_key"] == change_key
    tasks = await c8.query(url, "SELECT id,kind,status FROM tasks ORDER BY id")
    assert tasks == expected_tasks, tasks
    characters = await c8.query(url, "SELECT id FROM characters ORDER BY id")
    assert characters == []
    assert len(model.valid_keys) == expected_calls and all(model.valid_keys)
    assert model.unexpected == 0 and model.queue.empty()
    c8.mark(
        "原提案核对",
        case=case,
        stage=stage,
        db_revision=version,
        fields_equal=len(projected),
        original_row_sha256=digest(projected),
        extra=extra,
        original_task=row["turn_task_id"],
        status=row["status"],
        characters=len(characters),
        tasks=tasks,
        model_calls=len(model.valid_keys),
    )
    return extra.get("change_key")


async def rejected_actions(author, url, row, new):
    body = {"change_key": row["change_key"], "view_version": row["view_version"]} if new else None
    responses = {}
    for action in ("confirm", "reject"):
        response = await author.post(url + "/changes/" + str(row["id"]) + "/" + action, json=body)
        assert response.status_code == 409, (action, response.status_code)
        responses[action] = response.status_code
    return responses


async def run(shape, root, pg):
    c8.NEW = TARGET
    builds = await c8.prepare(root)
    old, new = builds["old"], builds["new"]
    key, jwt = Fernet.generate_key().decode(), secrets.token_hex(32)
    source = c8.url_for(root, shape, pg, "c8base")
    old_env = c8.app_env(root, old, source, key, jwt)
    await c8.migrate(old, old_env)
    assert await c8.version(source) == "0043"
    model = c8.Model()
    async with c8.fake_model(model) as model_url:
        async with c8.application(root, old, old_env, "old-seed") as (base, _):
            async with httpx.AsyncClient(base_url=base, timeout=30, trust_env=False) as author:
                await c8.register(author, c8.AUTHOR)
                await c8.configure(author, model_url)
                project = await c8.project(author, "旧提案核对")
                url = f"{c8.API}/projects/{project['id']}/agent"
                conversation = c8.checked(await author.post(url + "/conversations", json={}), 201)
                conversation_url = url + f"/conversations/{conversation['id']}"
                model.expect(tool=True)
                sent = c8.checked(
                    await author.post(conversation_url + "/messages", json={"content": "请提出虚构角色"}), 202
                )
                tid = sent["task"]["id"]
                await c8.task_state(author, tid, {"paused"})
                original = await changes(source)
                assert original["status"] == "proposed" and original["turn_task_id"] == tid
                assert original["tool_name"] == "create_character"
                assert original["executed_at"] is None and original["undone_at"] is None
                c8.mark(
                    "0043原提案字段",
                    columns=sorted(original),
                    fields_sha256={k: digest(v) for k, v in original.items()},
                    proposal_id=original["id"],
                    tool=original["tool_name"],
                    conversation=conversation["id"],
                )
        paused_task = [(tid, "agent.turn", "paused")]
        cancelled_task = [(tid, "agent.turn", "cancelled")]
        await compare(source, original, "source", "旧应用退出", paused_task, model, 1)
        paused = await c8.snapshot(root, shape, pg, source, "old_paused")
        for case in ("cancel_before_upgrade", "cancel_after_upgrade"):
            cold = paused
            if case == "cancel_before_upgrade":
                copy = await c8.restore(root, shape, pg, paused, "old_cancel")
                async with c8.application(root, old, c8.app_env(root, old, copy, key, jwt), "old-cancel") as (base, _):
                    async with aclosing(await c8.login(base, c8.AUTHOR)) as author:
                        c8.checked(await author.post(f"{c8.API}/tasks/{tid}/cancel"), 200)
                        await c8.task_state(author, tid, {"cancelled"})
                        detail = c8.checked(await author.get(conversation_url), 200)
                        assert detail["active_task"] is None and detail["changes"][0]["status"] == "proposed"
                        results = await rejected_actions(author, url, detail["changes"][0], False)
                        c8.mark("旧版取消后原提案操作", **results)
                await compare(copy, original, case, "旧版取消和正常退出", cancelled_task, model, 1)
                cold = await c8.snapshot(root, shape, pg, copy, "old_cancelled")
            instance = await c8.restore(root, shape, pg, cold, "new_" + case)
            expected = cancelled_task if case == "cancel_before_upgrade" else paused_task
            await compare(instance, original, case, "恢复到独立0043目标", expected, model, 1)
            env = c8.app_env(root, new, instance, key, jwt)
            await c8.migrate(new, env)
            ck = await compare(instance, original, case, "升级后启动前", expected, model, 1)
            count = 1
            async with c8.application(root, new, env, "new-" + case) as (base, _):
                async with aclosing(await c8.login(base, c8.AUTHOR)) as author:
                    if case == "cancel_after_upgrade":
                        task = (await c8.tasks_by_id(author))[tid]
                        c8.checked(
                            await author.post(f"{c8.API}/tasks/{tid}/cancel", params={"task_key": task["task_key"]}),
                            200,
                        )
                        await c8.task_state(author, tid, {"cancelled"}, task_key=task["task_key"])
                    detail = c8.checked(await author.get(conversation_url), 200)
                    assert detail["active_task"] is None and len(detail["changes"]) == 1
                    row = detail["changes"][0]
                    assert row["id"] == original["id"] and row["change_key"] == ck and row["status"] == "proposed"
                    assert row["turn_task_key"] is None
                    results = await rejected_actions(author, url, row, True)
                    c8.mark("新版取消后原提案操作", case=case, public_turn_task_id=row["turn_task_id"], **results)
                    await compare(instance, original, case, "取消、单项确认与拒绝后", cancelled_task, model, 1, ck)
                    expected = cancelled_task
                    if case == "cancel_after_upgrade":
                        model.expect(text="这是独立的新轮次")
                        sent = c8.checked(
                            await author.post(conversation_url + "/messages", json={"content": "请仅回复文本"}), 202
                        )
                        successor = sent["task"]
                        assert successor["id"] != tid
                        await c8.task_state(author, successor["id"], {"succeeded"}, task_key=successor["task_key"])
                        expected = [*cancelled_task, (successor["id"], "agent.turn", "succeeded")]
                        count = 2
                        await compare(instance, original, case, "同会话新消息单独成功", expected, model, count, ck)
            async with c8.application(root, new, env, "restart-" + case) as (base, _):
                async with aclosing(await c8.login(base, c8.AUTHOR)) as author:
                    detail = c8.checked(await author.get(conversation_url), 200)
                    assert detail["active_task"] is None and len(detail["changes"]) == 1
                    row = detail["changes"][0]
                    assert (row["id"], row["change_key"], row["status"]) == (original["id"], ck, "proposed")
                    await compare(instance, original, case, "新进程重启并读取原会话", expected, model, count, ck)
        c8.mark("双场景完成", engine=shape, unexpected_calls=model.unexpected, attempts=len(model.valid_keys))


async def main(shape):
    c8.mark(
        "提案演练",
        engine=shape,
        old=c8.OLD,
        new=TARGET,
        script_sha256=hashlib.sha256(await asyncio.to_thread(Path(__file__).read_bytes)).hexdigest(),
        controller_sha256=hashlib.sha256(await asyncio.to_thread(Path(c8.__file__).read_bytes)).hexdigest(),
        sqlite_library=sqlite3.sqlite_version if shape == "sqlite" else None,
    )
    with TemporaryDirectory(prefix="ainovel-34-proposal-") as tmp:
        if shape == "postgres":
            async with c8.postgres() as pg:
                await run(shape, Path(tmp), pg)
        else:
            await run(shape, Path(tmp), None)
    assert not await asyncio.to_thread(Path(tmp).exists)
    c8.mark("专属临时目录已清理", absent=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shape", choices=("sqlite", "postgres"), required=True)
    asyncio.run(main(parser.parse_args().shape))
