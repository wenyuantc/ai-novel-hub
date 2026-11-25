"""用受控 Docker 响应验证创建中断和容器归属／清理门禁。"""

from __future__ import annotations

import asyncio
import contextlib
import io
from unittest.mock import patch

import c8_drill as c8

CID = "a" * 64
OTHER = "b" * 64
NAME = "ainovel-c8-controlled"
OWNER = "fictional-owner"


async def check(*, known, initial, after, label, remove_exit, expected) -> None:
    calls = []
    list_calls = 0

    async def fake_run(args, **_kwargs):
        nonlocal list_calls
        calls.append(args)
        if args[:3] == ["docker", "ps", "-a"]:
            list_calls += 1
            state = (
                initial if list_calls == 1 or not any(call[:3] == ["docker", "rm", "-f"] for call in calls) else after
            )
            if state is None:
                raise RuntimeError("受控查询故障")
            return 0, state.encode(), b""
        if args[:2] == ["docker", "inspect"]:
            if label is None:
                raise RuntimeError("受控标签查询故障")
            return 0, label.encode(), b""
        assert args == ["docker", "rm", "-f", "-v", CID]
        if remove_exit is None:
            raise RuntimeError("受控删除故障")
        return remove_exit, b"", b""

    c8.EVENTS.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            await c8.remove_postgres_container(NAME, OWNER, CID if known else None, run=fake_run)
        except RuntimeError as exc:
            assert expected != "absent" and NAME in str(exc)
        else:
            assert expected == "absent"
    event = c8.EVENTS[-1]
    assert event["container"] == NAME
    if expected == "absent":
        assert event["phase"] == "专属数据库清理完成" and event["verified_absent"] is True
    else:
        assert event["phase"] == "专属数据库清理失败" and event["reason"] == expected
    deletes = [call for call in calls if call[:3] == ["docker", "rm", "-f"]]
    if label != OWNER or initial in ("", None):
        assert not deletes
    else:
        assert len(deletes) <= 1


async def interrupted_creation(mode: str) -> None:
    created = asyncio.Event()
    removed = False
    own_name = own_label = ""

    async def fake_run(args, **_kwargs):
        nonlocal removed, own_name, own_label
        if args[:2] == ["docker", "run"]:
            own_name = args[args.index("--name") + 1]
            label = args[args.index("--label") + 1]
            assert label.startswith(c8.OWNER_LABEL + "=")
            assert args[args.index("--tmpfs") + 1] == "/var/lib/postgresql/data:rw,size=1g"
            own_label = label.split("=", 1)[1]
            created.set()
            if mode == "timeout":
                raise TimeoutError("受控创建超时")
            await asyncio.Future()
        if args[:3] == ["docker", "ps", "-a"]:
            return 0, b"" if removed else f"{CID}\t{own_name}\n".encode(), b""
        if args[:2] == ["docker", "inspect"]:
            return 0, own_label.encode(), b""
        assert args == ["docker", "rm", "-f", "-v", CID]
        removed = True
        return 0, b"", b""

    async def start():
        async with c8.postgres():
            raise AssertionError("创建不应完成")

    c8.EVENTS.clear()
    with patch.object(c8, "command", fake_run), contextlib.redirect_stdout(io.StringIO()):
        task = asyncio.create_task(start())
        await asyncio.wait_for(created.wait(), 2)
        if mode == "cancel":
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, TimeoutError) as exc:
            assert isinstance(exc, asyncio.CancelledError if mode == "cancel" else TimeoutError)
        else:
            raise AssertionError("创建取消未传播")
    assert removed
    event = c8.EVENTS[-1]
    assert event["phase"] == "专属数据库清理完成"
    assert event["container"] == own_name and event["container_id"] == CID and not event["creation_id_known"]


async def main() -> None:
    present = f"{CID}\t{NAME}\n"
    other = f"{OTHER}\t{NAME}\n"
    await check(known=True, initial=present, after="", label=OWNER, remove_exit=0, expected="absent")
    await check(known=True, initial="", after="", label=OWNER, remove_exit=1, expected="absent")
    await check(known=True, initial=present, after="", label=OWNER, remove_exit=1, expected="absent")
    await check(known=True, initial=present, after=present, label=OWNER, remove_exit=1, expected="still_exists")
    await check(known=True, initial=present, after=present, label=OWNER, remove_exit=None, expected="still_exists")
    await check(
        known=True, initial=present, after=None, label=OWNER, remove_exit=0, expected="query_failed_after_remove"
    )
    await check(known=True, initial=None, after="", label=OWNER, remove_exit=0, expected="query_failed")
    await check(known=True, initial=present, after="", label=None, remove_exit=0, expected="label_query_failed")
    await check(known=True, initial=present, after="", label="foreign", remove_exit=0, expected="not_owned")
    await check(known=True, initial=other, after="", label=OWNER, remove_exit=0, expected="identity_conflict")
    await check(known=False, initial=present, after="", label=OWNER, remove_exit=0, expected="absent")
    await check(known=False, initial="", after="", label=OWNER, remove_exit=0, expected="creation_outcome_unconfirmed")
    await interrupted_creation("cancel")
    await interrupted_creation("timeout")
    print("创建取消或超时后按标签回收；已不存在经查询确认；不归属、仍存在及查询不明均失败并报告身份。")


if __name__ == "__main__":
    asyncio.run(main())
