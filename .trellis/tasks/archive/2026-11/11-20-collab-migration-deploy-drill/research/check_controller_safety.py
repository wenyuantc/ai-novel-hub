"""核验演练控制器导入时不打开 .env，且命令故障不暴露原始输出。"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import io
import os
import secrets
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def audit(event: str, args: tuple) -> None:
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        if Path(os.fsdecode(args[0])).name == ".env":
            raise RuntimeError("禁止打开 .env")


def main() -> None:
    sys.addaudithook(audit)
    with TemporaryDirectory(prefix="ainovel-c8-audit-") as temp:
        try:
            (Path(temp) / ".env").open()
        except RuntimeError:
            pass
        else:
            raise AssertionError("审计钩子没有生效")
    modules = (
        "c8_drill",
        "backup_drain",
        "legacy_agent_followup",
        "legacy_cancel_matrix",
        "prepare_maintenance_proxy",
        "prepare_new_frontend",
        "prepare_old_frontend",
    )
    imported = {name: importlib.import_module(name) for name in modules}
    controller = imported["c8_drill"]
    marker = secrets.token_hex(24)
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        try:
            asyncio.run(
                controller.command(
                    [sys.executable, "-c", "import sys; print(sys.argv[1], file=sys.stderr); sys.exit(17)", marker],
                    env=controller.safe_env(),
                )
            )
        except RuntimeError as exc:
            assert str(exc) == f"command_failed:{Path(sys.executable).name}:17"
            assert marker not in str(exc)
        else:
            raise AssertionError("预期的命令失败未出现")
    assert marker not in stream.getvalue()
    print("控制器导入未打开 .env；受控子进程故障只暴露固定类别及退出码。")


if __name__ == "__main__":
    main()
