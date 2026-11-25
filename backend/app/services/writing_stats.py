"""只统计保存成功后读者正文实际写入的非空白字符。"""

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.writing_stats import WritingDailyStat

BEIJING = ZoneInfo("Asia/Shanghai")
_ALIGNMENT_WIDTH = 2048
_FULL_ALIGNMENT_WIDTH = 8192
_FULL_ALIGNMENT_CELLS = 64_000_000
_ANCHOR_LENGTHS = (32, 16, 8, 4)


def _extend_repeated_anchor(before: str, after: str, start: int, stop: int) -> tuple[int, int, int]:
    # “甲乙”一类周期串在许多位置上都能匹配短锚点。向右、再向左延展至完整连续片段，
    # 最后才在等价位置中选最近者，避免把不等长的两端替换误计到中间的原文里。
    low, high = stop, len(before)
    while low < high:
        mid = (low + high + 1) // 2
        if before[start:mid] in after:
            low = mid
        else:
            high = mid - 1
    stop = low
    low, high = 0, start
    while low < high:
        mid = (low + high) // 2
        if before[mid:stop] in after:
            high = mid
        else:
            low = mid + 1
    start = low
    anchor = before[start:stop]
    expected = start * len(after) // len(before)
    left = after.rfind(anchor, 0, expected + len(anchor))
    right = after.find(anchor, expected)
    found = left if right < 0 or (left >= 0 and expected - left <= right - expected) else right
    return start, stop, found


def _split_large_block(before: str, after: str) -> int:
    middle = len(before) // 2
    # 中段校字越密，可用的连续原文就越短。短锚点只认新旧文中都唯一的片段，
    # 避免低频错字叠加大段插入时，按比例切断仍然保留的字。
    for size in _ANCHOR_LENGTHS:
        for start in (middle, middle - size, middle + size):
            stop = start + size
            anchor = before[start:stop]
            found = after.find(anchor)
            if found < 0:
                continue
            repeated = after.find(anchor, found + 1) >= 0
            if size < _ANCHOR_LENGTHS[0] and (repeated or before.find(anchor) != before.rfind(anchor)):
                continue
            if repeated:
                start, stop, found = _extend_repeated_anchor(before, after, start, stop)
            return (
                _added_in_block(before[:start], after[:found])
                + _added_in_block(before[stop:], after[found + stop - start:])
            )

    # 大范围重写且找不到可确认的文字锚点，按相对位置切成有界的块再对齐。
    split = middle * len(after) // len(before)
    return _added_in_block(before[:middle], after[:split]) + _added_in_block(before[middle:], after[split:])


def _added_in_block(before: str, after: str, *, full_text: bool = False) -> int:
    if after.startswith(before):
        return len(after) - len(before)
    if before.startswith(after):
        return 0

    start = 0
    limit = min(len(before), len(after))
    while start < limit and before[start] == after[start]:
        start += 1
    end = 0
    while end < limit - start and before[-end - 1] == after[-end - 1]:
        end += 1
    before = before[start:len(before) - end]
    after = after[start:len(after) - end]
    if not before or not after:
        return len(after)

    width = min(len(before), len(after))
    # 普通章节在预算内做全局精确对齐，即使通篇打乱，也可得到真实最长公共子序列。
    # 超出预算的长文才分块，递归后每个局部位图宽度仍限于 2048。
    within_budget = full_text and width <= _FULL_ALIGNMENT_WIDTH and len(before) * len(after) <= _FULL_ALIGNMENT_CELLS
    if width > _ALIGNMENT_WIDTH and not within_budget:
        return _split_large_block(before, after)

    # 位并行 LCS。每一位表示对应列的最长公共子序列长度比上一列大 1；
    # 只求长度，不建立正文长度平方大小的矩阵，也不会忽略重复汉字。
    shorter, longer = (before, after) if len(before) <= len(after) else (after, before)
    masks: dict[str, int] = {}
    for index, char in enumerate(shorter):
        masks[char] = masks.get(char, 0) | (1 << index)
    row = 0
    for char in longer:
        matches = row | masks.get(char, 0)
        row = matches & ~(matches - ((row << 1) | 1))
    return len(after) - row.bit_count()


def count_added_words(before: str, after: str) -> int:
    if before == after:
        return 0
    old = ["".join(line.split()) for line in before.splitlines()]
    new = ["".join(line.split()) for line in after.splitlines()]
    # 两端未改的段落先整体裁剪。正文内部有重复段时，单纯按段贪心匹配会认错同一段。
    start = 0
    limit = min(len(old), len(new))
    while start < limit and old[start] == new[start]:
        start += 1
    end = 0
    while end < limit - start and old[-end - 1] == new[-end - 1]:
        end += 1
    old_text = "".join(old[start:len(old) - end])
    new_text = "".join(new[start:len(new) - end])
    if new_text.startswith(old_text):
        return len(new_text) - len(old_text)
    if old_text.startswith(new_text):
        return 0

    # 两侧独有的字不可能保留。先在全篇扣掉它们，避免长文插入一段再逐行校字时
    # 把剩余原文因分块错位当作新字；若共有部分是子序列，字数已可直接确定。
    common = set(old_text).intersection(new_text)
    old_shared = "".join(c for c in old_text if c in common)
    new_shared = "".join(c for c in new_text if c in common)
    unique_additions = len(new_text) - len(new_shared)
    shorter, longer = (old_shared, new_shared) if len(old_shared) <= len(new_shared) else (new_shared, old_shared)
    remaining = iter(longer)
    if all(char in remaining for char in shorter):
        return unique_additions + max(0, len(new_shared) - len(old_shared))
    return unique_additions + _added_in_block(old_shared, new_shared, full_text=True)


async def record_words(
    db: AsyncSession, *, user_id: int, project_id: int, source: Literal["manual", "ai"], words: int,
) -> None:
    """与正文共用当前事务。数据库原子递增，调用方负责权限、版本校验及提交。"""
    if words < 0 or source not in {"manual", "ai"}:
        raise ValueError("写作字数或来源无效")
    if not words:
        return
    stat = WritingDailyStat
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    field = stat.manual_words if source == "manual" else stat.ai_words
    if not db.in_transaction():
        await db.begin()
    transaction = db.get_transaction()
    previous, day = db.info.get("writing_stats_day", (None, None))
    if previous is not transaction:
        day = datetime.now(BEIJING).date()
        db.info["writing_stats_day"] = (transaction, day)
    statement = insert(stat).values(
        user_id=user_id, project_id=project_id, day=day, **{field.key: words},
    )
    await db.execute(statement.on_conflict_do_update(
        index_elements=[stat.user_id, stat.project_id, stat.day], set_={field.key: field + words},
    ))
