"""同一组备份归属和受理竞争在 PostgreSQL 上验证实际行锁。"""

import sys

import pytest

from tests.schema_helpers import isolated_worker_database
from tests.test_c5_backup import (
    test_only_one_database_claim_can_execute_and_notify,
    test_peer_recovers_an_abandoned_run_and_an_abandoned_recovery,
    test_peers_competing_for_same_schedule_treat_loser_as_noop,
)

__all__ = [
    "test_only_one_database_claim_can_execute_and_notify",
    "test_peer_recovers_an_abandoned_run_and_an_abandoned_recovery",
    "test_peers_competing_for_same_schedule_treat_loser_as_noop",
]


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch, required=True) as overrides:
        # 三例只验证数据库边界，真正的 pg_dump 由整库备份适配器验收。
        yield {**overrides, "backup_pg_dump_path": sys.executable}
