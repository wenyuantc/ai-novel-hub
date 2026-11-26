"""真实 PostgreSQL 下执行相同的日期读写事务与并发冲突用例。"""

import pytest

from tests.schema_helpers import isolated_worker_database
from tests.test_story_timeline import (
    test_dates_edit_permissions_and_retired_corrections,
    test_narrative_flashback_unknown_and_same_day_pagination,
    test_same_revision_concurrent_saves,
)

__all__ = [
    "test_dates_edit_permissions_and_retired_corrections", "test_narrative_flashback_unknown_and_same_day_pagination",
    "test_same_revision_concurrent_saves",
]


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch, required=True) as overrides:
        yield overrides
