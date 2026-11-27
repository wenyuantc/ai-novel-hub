"""用 0043 的库和真实 HTTP 入口核对旧章节。只写入专属快照副本。"""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import signal
import sys
from contextlib import aclosing
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from cryptography.fernet import Fernet

TASKS = Path(__file__).resolve().parents[2]
C8 = TASKS / "archive/2026-11/11-20-collab-migration-deploy-drill/research"
sys.path.insert(0, str(C8))
import c8_drill as drill  # noqa: E402
from legacy_crash_drill import verify_port_closed  # noqa: E402

drill.NEW = "ac97d4de084c60c9b50df32143623ae34cc9c546"
TARGET_REVISION = "0079"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


async def content(url, chapter):
    (row,) = await drill.query(
        url,
        f"SELECT title,content,word_count,content_revision FROM chapters WHERE id={chapter}",
    )
    scenes = await drill.query(
        url,
        f"SELECT id,chapter_id,title,content,word_count,summary,sort,separator_before,active "
        f"FROM scenes WHERE chapter_id={chapter} ORDER BY id",
    )
    versions = await drill.query(
        url,
        f"SELECT id,chapter_id,seq,source,content,scene_snapshot,word_count,starred,"
        f"note,generation_log_id FROM chapter_versions WHERE chapter_id={chapter} ORDER BY id",
    )
    assert row[1] and scenes and versions
    return dict(
        text_sha=digest(row[:3]),
        revision=row[3],
        scenes=len(scenes),
        scenes_sha=digest(scenes),
        versions=len(versions),
        versions_sha=digest(versions),
    )


async def state(url, ch, task):
    rows = await drill.query(
        url,
        f"SELECT status,generation_claim_key,generation_claim_task_key,generation_claim_prev_status "
        f"FROM chapters WHERE id={ch}",
    )
    ((status, *claim),) = rows
    (row,) = await drill.query(
        url,
        f"SELECT id,statistics_key,status,worker_key,execution_key,admin_cancel_operation_id,"
        f"serial_key,resource_key,project_id FROM tasks WHERE id={task}",
    )
    peers = await drill.query(
        url,
        f"SELECT id,status FROM tasks WHERE id<>{task} AND "
        f"(chapter_id={ch} OR serial_key=(SELECT serial_key FROM tasks WHERE id={task}) "
        f"OR resource_key=(SELECT resource_key FROM tasks WHERE id={task})) ORDER BY id",
    )
    work = await drill.query(
        url,
        "SELECT kind,state,task_id,task_key,worker_key,execution_key,operation_id,evidence "
        f"FROM task_work_records WHERE project_id={int(row[8])} OR task_id={task} ORDER BY work_key",
    )
    total = {}
    for table, cond in (
        ("scenes", f"chapter_id={ch}"),
        ("chapter_versions", f"chapter_id={ch}"),
        ("tasks", f"id={task}"),
    ):
        total[table] = digest(
            await drill.query(url, f"SELECT * FROM {table} WHERE {cond} ORDER BY id")
        )
    return dict(
        chapter=status,
        claim=claim,
        task=list(row[:6]),
        peers=peers,
        work=work,
        work_sha=digest(
            await drill.query(url, "SELECT * FROM task_work_records ORDER BY work_key")
        ),
        slots=await drill.query(
            url,
            "SELECT task_id,execution_key FROM task_execution_slots ORDER BY task_id",
        ),
        all_tasks=await drill.query(url, "SELECT id,status FROM tasks ORDER BY id"),
        full_rows=total,
        content=await content(url, ch),
    )


def allowed(facts, baseline, stopped_takeover_worker=None):
    # This branch is only reachable with independent cold-fixture provenance and a verified exited recovery PID.
    passive_failure = (
        stopped_takeover_worker is not None
        and facts["task"][3] == stopped_takeover_worker
        and facts["work"]
        == [("abort_failure", "failed", None, None, None, None, None, None)]
        and facts["all_tasks"] == [(facts["task"][0], "interrupted")]
    )
    return bool(
        baseline
        and baseline["status"] in ("draft", "done")
        and baseline["content"] == facts["content"]
        and facts["chapter"] == "generating"
        and facts["claim"] == [None] * 3
        and facts["task"][2] == "interrupted"
        and facts["task"][4:] == [None, None]
        and not facts["peers"]
        and not facts["slots"]
        and ((facts["task"][3] is None and not facts["work"]) or passive_failure)
    )


async def kill_old(base, proc, call):
    assert (
        os.getpgid(proc.pid) == proc.pid != os.getpgrp() and not call.release.is_set()
    )
    proc.kill()
    assert await asyncio.wait_for(proc.wait(), 15) == -signal.SIGKILL
    await verify_port_closed(base)
    await asyncio.wait_for(call.closed.wait(), 10)
    drill.mark(
        "旧执行退出",
        pid=proc.pid,
        exit_code=proc.returncode,
        listener_closed=True,
        model_stream_closed=True,
    )


async def exercise(shape, root, pg, original_status):
    versions = await drill.prepare(root)
    old, new = versions["old"], versions["new"]
    key, jwt = Fernet.generate_key().decode(), secrets.token_hex(32)
    url = drill.url_for(root, shape, pg, "c8base")

    def env(ver, target):
        return drill.app_env(root, ver, target, key, jwt)

    await drill.migrate(old, env(old, url))
    assert await drill.version(url) == "0043"
    model = drill.Model()
    async with drill.fake_model(model) as model_url:
        async with drill.application(root, old, env(old, url), "seed") as (base, _):
            async with httpx.AsyncClient(base_url=base, trust_env=False) as author:
                await drill.register(author, drill.AUTHOR)
                await drill.configure(author, model_url)
                pid = (await drill.project(author, "章节复位演练"))["id"]
                ch = (
                    await drill.chapter(
                        author, pid, "原章节", "第一段真实保存的演练正文。"
                    )
                )["id"]
                blocker = (
                    await drill.chapter(author, pid, "排队对照用的前置章", "前置正文。")
                )["id"]
                route = f"{drill.API}/projects/{pid}/chapters/{ch}"
                drill.checked(
                    await author.patch(route, json={"status": original_status}), 200
                )
                chapter = drill.checked(await author.get(route), 200)
                baseline = dict(
                    status=chapter["status"], content=await content(url, ch)
                )
                assert baseline["status"] == original_status and (
                    await drill.query(url, "SELECT count(*) FROM tasks")
                ) == [(0,)]
                drill.mark(
                    "生成前业务基线",
                    project=pid,
                    chapter=ch,
                    active_tasks=0,
                    **baseline,
                )
        clean = await drill.snapshot(root, shape, pg, url, "old_drained")
        # 独立旧库产生冷快照与在线排队快照，避免正例携带另一项仍在队列的任务。
        coldsrc = await drill.restore(root, shape, pg, clean, "coldsource")
        async with drill.application(root, old, env(old, coldsrc), "old-running") as (
            base,
            proc,
        ):
            async with aclosing(await drill.login(base, drill.AUTHOR)) as author:
                active, call = await drill.generate(author, model, ch, gated=True)
                tid = int(active["id"])
                await drill.task_state(author, tid, {"running"})
                assert await content(coldsrc, ch) == baseline["content"]
                assert await drill.query(
                    coldsrc, f"SELECT status FROM chapters WHERE id={ch}"
                ) == [("generating",)]
                await kill_old(base, proc, call)
        assert await drill.group_members(proc.pid) == set()
        assert await drill.query(
            coldsrc, f"SELECT status FROM tasks WHERE id={tid}"
        ) == [("running",)]
        cold = await drill.snapshot(root, shape, pg, coldsrc, "old_cold")
        if original_status == "done":
            queuesrc = await drill.restore(root, shape, pg, clean, "queuesource")
            async with drill.application(
                root,
                old,
                {
                    **env(old, queuesrc),
                    "TASK_MAX_CONCURRENCY": "1",
                    "TASK_MAX_PER_USER": "1",
                },
                "old-queue",
            ) as (base, proc):
                async with aclosing(await drill.login(base, drill.AUTHOR)) as author:
                    ahead, call = await drill.generate(
                        author, model, blocker, gated=True
                    )
                    task = drill.checked(
                        await author.post(
                            f"{drill.API}/chapters/{ch}/generate", json={}
                        ),
                        202,
                    )
                    qid = task["id"]
                    await drill.task_state(author, qid, {"queued"})
                    queued = await drill.snapshot(
                        root, shape, pg, queuesrc, "old_online_queue"
                    )
                    await kill_old(base, proc, call)
            assert await drill.group_members(proc.pid) == set()

        cases = (
            ("drained", "queued", "missing", "reset", "successor")
            if original_status == "done"
            else ("reset",)
        )
        for case in cases:
            snapshot = (
                clean if case == "drained" else queued if case == "queued" else cold
            )
            target = await drill.restore(root, shape, pg, snapshot, case)
            assert await drill.version(target) == "0043"
            assert await content(target, ch) == baseline["content"]
            old_tasks = await drill.query(
                target, "SELECT id,chapter_id,status FROM tasks ORDER BY id"
            )
            expected_tasks = [] if case == "drained" else [(tid, ch, "running")]
            if case == "queued":
                expected_tasks = [
                    (ahead["id"], blocker, "running"),
                    (qid, ch, "queued"),
                ]
            assert old_tasks == expected_tasks, (case, old_tasks)
            drill.mark(
                "迁移前复读快照",
                case=case,
                revision="0043",
                tasks=old_tasks,
                calls=len(model.valid_keys),
                unexpected=model.unexpected,
            )
            assert model.unexpected == 0
            await drill.migrate(new, env(new, target))
            assert await drill.version(target) == TARGET_REVISION
            before_calls = len(model.valid_keys)
            expected_calls = int(case == "successor")
            recovery_worker = None
            if case in ("reset", "successor"):
                async with drill.application(
                    root, new, env(new, target), "takeover-" + case
                ) as (recovery_base, recovery):
                    async with asyncio.timeout(20):
                        while True:
                            observed = await state(target, ch, tid)
                            if (
                                observed["task"][2] == "interrupted"
                                and observed["work"]
                            ):
                                break
                            await asyncio.sleep(0.2)
                    assert not allowed(observed, baseline)
                    drill.mark(
                        "原门禁拒绝，保留失败记录",
                        case=case,
                        reset_requests=0,
                        **observed,
                    )
                await verify_port_closed(recovery_base)
                assert not await drill.group_members(recovery.pid)
                workers = await drill.query(
                    target,
                    f"SELECT worker_key FROM task_workers WHERE pid={recovery.pid} AND stopped_at IS NOT NULL",
                )
                assert (observed["task"][3],) in workers
                recovery_worker = observed["task"][3]
                drill.mark(
                    "接管进程退出",
                    pid=recovery.pid,
                    worker=recovery_worker,
                    group_empty=True,
                    listener_closed=True,
                    worker_stopped=True,
                )
            async with drill.application(
                root, new, env(new, target), "new-" + case
            ) as (base, _):
                async with aclosing(await drill.login(base, drill.AUTHOR)) as author:
                    if case == "drained":
                        chapter = drill.checked(await author.get(route), 200)
                        assert (
                            chapter["status"] == baseline["status"]
                            and await content(target, ch) == baseline["content"]
                        )
                        assert (
                            await drill.query(target, "SELECT count(*) FROM tasks")
                        ) == [(0,)]
                        drill.mark(
                            "排空对照升级",
                            revision=TARGET_REVISION,
                            tasks=0,
                            chapter=chapter["status"],
                        )
                        continue
                    ident = qid if case == "queued" else tid
                    facts = await state(target, ch, ident)
                    assert facts["content"] == baseline["content"]
                    if case == "queued":
                        assert not allowed(facts, baseline)
                        drill.mark("排队快照在新版的初次观察", **facts)
                        response = None
                        if facts["task"][2] == "queued":
                            async with aclosing(
                                await drill.login(base, drill.ADMIN)
                            ) as admin:
                                response = await admin.post(
                                    f"{drill.API}/admin/tasks/{facts['task'][1]}/cancel",
                                    json={
                                        "confirm": True,
                                        "reason": "隔离验证旧章节排队收尾",
                                    },
                                )
                            assert response.status_code in (202, 409)
                        await asyncio.sleep(12)
                        facts = await state(target, ch, ident)
                        assert (
                            facts["chapter"] == "generating"
                            and facts["content"] == baseline["content"]
                        )
                        drill.mark(
                            "排队旧章停止处置",
                            http_cancel=response.status_code
                            if response is not None
                            else None,
                            operation=response.json()["operation_id"]
                            if response is not None and response.status_code == 202
                            else None,
                            wait_s=12,
                            reset_requests=0,
                            **facts,
                        )
                    elif case == "missing":
                        assert (
                            not allowed(facts, None)
                            and facts["chapter"] == "generating"
                        )
                        drill.mark(
                            "缺少生成前状态的操作者停止处置", reset_requests=0, **facts
                        )
                    else:
                        assert allowed(facts, baseline, recovery_worker), facts
                        assert (
                            observed["work_sha"] == facts["work_sha"]
                            and observed["full_rows"] == facts["full_rows"]
                        )
                        drill.mark("复位前", case=case, **facts)
                        response = drill.checked(
                            await author.patch(
                                route, json={"status": baseline["status"]}
                            ),
                            200,
                        )
                        assert response["status"] == baseline["status"]
                        restored = await state(target, ch, ident)
                        assert restored == {**facts, "chapter": baseline["status"]}, (
                            restored
                        )
                        drill.mark(
                            "作者单字段复位后", case=case, http_patch=200, **restored
                        )
                        if case == "successor":
                            following, call = await drill.generate(
                                author, model, ch, gated=True
                            )
                            try:
                                occupied = await state(target, ch, ident)
                                assert (
                                    occupied["claim"][1] == following["task_key"]
                                    and occupied["peers"]
                                )
                                assert not allowed(occupied, baseline, recovery_worker)
                                drill.mark(
                                    "真实后继有新占用，停止旧章处置",
                                    additional_reset_requests=0,
                                    **occupied,
                                )
                            finally:
                                call.release.set()
                            final_task = await drill.task_state(
                                author,
                                following["id"],
                                {"succeeded", "failed"},
                                task_key=following["task_key"],
                            )
                            assert final_task["status"] == "succeeded"
                            drill.mark(
                                "后继自行完成",
                                task=following["id"],
                                status=final_task["status"],
                            )
                            continue
            if case != "drained":
                async with drill.application(
                    root, new, env(new, target), "restart-" + case
                ):
                    await asyncio.sleep(3)
                    again = await state(target, ch, ident)
                    if case == "queued":
                        assert (
                            again["content"] == facts["content"]
                            and again["chapter"] == "generating"
                        )
                    else:
                        assert again == (restored if case == "reset" else facts), again
                    assert (
                        len(model.valid_keys) - before_calls == expected_calls
                        and model.unexpected == 0
                    )
                    drill.mark(
                        "重启复读",
                        case=case,
                        observe_s=3,
                        model_requests_delta=len(model.valid_keys) - before_calls,
                        **again,
                    )
        assert (
            model.unexpected == 0
            and all(model.valid_keys)
            and len(model.valid_keys) == (3 if original_status == "done" else 1)
        )
        drill.mark(
            "章节双向核对完成",
            database=shape,
            revision=TARGET_REVISION,
            expected_model_calls=len(model.valid_keys),
            unexpected_model_calls=model.unexpected,
        )


async def main(shape, original_status):
    with TemporaryDirectory(prefix="ainovel-chapter34-") as temp:
        root = Path(temp)
        drill.mark(
            "章节演练启动",
            shape=shape,
            original_status=original_status,
            controller_sha256=hashlib.sha256(
                await asyncio.to_thread(Path(__file__).read_bytes)
            ).hexdigest(),
            c8_sha256=hashlib.sha256(
                await asyncio.to_thread(Path(drill.__file__).read_bytes)
            ).hexdigest(),
        )
        if shape == "postgres":
            async with drill.postgres() as pg:
                await exercise(shape, root, pg, original_status)
        else:
            await exercise(shape, root, None, original_status)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("database", choices=["sqlite", "postgres"])
    parser.add_argument("--original-status", choices=["done", "draft"], default="done")
    args = parser.parse_args()
    asyncio.run(main(args.database, args.original_status))
