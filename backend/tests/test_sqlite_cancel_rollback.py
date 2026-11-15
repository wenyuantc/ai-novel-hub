"""取消打断 aiosqlite 语句后，作废连接不能以「僵尸连接」形式继续持有写锁。

回归：协作撤权取消执行体时恰好打断发送租约中的语句（写事务内），SQLAlchemy 作废连接并直接关闭，
未 finalize 的语句被异常回溯引用，sqlite3 关闭后写锁一直保留，占用回退与任务收尾全部 database is locked，
任务永久停在 running（`test_revoke_after_claim_before_send_never_sends[*-send]` 偶发卡死）。
"""

import asyncio
import sqlite3

from sqlalchemy import text

from app.core.database import create_engine, create_sessionmaker

# 足够慢的单步查询：首个 step 需要在驱动线程里算完整个递归，取消必然落在执行途中；
# 连接真实表使该语句持有库文件读锁（未 finalize 时即使回滚也仍阻止其他连接提交）。
_SLOW = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 3000000) "
    "SELECT count(*) FROM c, t"
)


async def test_cancelled_statement_in_write_transaction_releases_lock(tmp_path):
    path = tmp_path / "cancel.db"
    engine = create_engine(f"sqlite+aiosqlite:///{path}")
    maker = create_sessionmaker(engine)
    try:
        async with maker() as db:
            await db.execute(text("CREATE TABLE t (x INTEGER)"))
            await db.execute(text("INSERT INTO t VALUES (1)"))
            await db.commit()

        started = asyncio.Event()
        kept: list[BaseException] = []

        async def interrupted():
            async with maker() as db:
                # 写事务：no-op UPDATE 取得 RESERVED 锁，与发送租约的 User 屏障相同。
                await db.execute(text("UPDATE t SET x = x"))
                started.set()
                try:
                    await db.execute(text(_SLOW))
                except asyncio.CancelledError as exc:
                    # 保留异常（含回溯与游标引用），模拟真实收尾期间异常链仍存活、未被 GC。
                    kept.append(exc)
                    raise

        work = asyncio.create_task(interrupted())
        await started.wait()
        await asyncio.sleep(0.05)
        work.cancel()
        try:
            await work
        except asyncio.CancelledError:
            pass
        assert kept, "取消必须落在执行中的语句上"

        def acquire_write() -> None:
            other = sqlite3.connect(path, timeout=2.0)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("UPDATE t SET x = 2")
                other.commit()
            finally:
                other.close()

        # 被打断连接已作废；其他连接必须能在有限时间内取得写锁（修复前此处 database is locked）。
        await asyncio.to_thread(acquire_write)
        async with maker() as db:
            assert await db.scalar(text("SELECT x FROM t")) == 2
    finally:
        await engine.dispose()
