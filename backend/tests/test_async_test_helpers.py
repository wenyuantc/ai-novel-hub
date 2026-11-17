"""等待后台工作的测试辅助函数必须让完成回调得到执行。"""

import subprocess
import sys
from pathlib import Path
from textwrap import dedent


def test_drain_checks_yields_to_completion_callbacks():
    # 协程忙循环会阻塞 asyncio.timeout；用独立进程的超时确保回归本身不会挂死整套测试。
    program = dedent("""
        import asyncio
        from types import SimpleNamespace
        from tests.helpers import drain_collaboration_checks

        async def main():
            completed = []

            async def work(label):
                completed.append(label)

            first = asyncio.create_task(work("first"))
            await first
            checks = {first}

            def finished(task):
                checks.discard(task)
                follow_up = asyncio.create_task(work("follow-up"))
                checks.add(follow_up)
                follow_up.add_done_callback(checks.discard)

            # 任务已完成，但集合清理回调仍等待下一轮事件循环。
            first.add_done_callback(finished)
            await drain_collaboration_checks(SimpleNamespace(_collaboration_checks=checks))
            assert not checks
            assert completed == ["first", "follow-up"]

        asyncio.run(main())
    """)
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
