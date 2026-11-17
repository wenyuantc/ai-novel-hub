"""事实台账的纯函数：主体归一化、引句定位、模型输出规范化。不访问数据库。"""

import unicodedata
from typing import Any

from pydantic import ValidationError

from app.models.consistency import CONSISTENCY_CATEGORIES
from app.schemas.consistency import (
    MAX_FACTS_PER_CHAPTER,
    QUOTE_LIMIT,
    STATEMENT_LIMIT,
    SUBJECT_LIMIT,
    TIME_HINT_LIMIT,
    LedgerFact,
)


def subject_key(subject: str) -> str:
    """分组用的归一化键：全半角归一（NFKC）、去掉全部空白、大小写折叠。只用于本地分组，不改写原名。"""
    text = unicodedata.normalize("NFKC", subject or "")
    return "".join(text.split()).casefold()[:SUBJECT_LIMIT]


def locate_quote(content: str, quote: str) -> tuple[int, int] | None:
    """在正文中逐字查找引句的首个匹配，返回码点半开区间；找不到（改写、引自作者区块）返回 None。

    Python 字符串下标即码点下标，emoji 等非 BMP 字符按一个位置计。不做任何模糊匹配。
    """
    if not quote:
        return None
    start = content.find(quote)
    return None if start < 0 else (start, start + len(quote))


def _text(value: Any, limit: int) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return ""
    return str(value).strip()[:limit].strip()


def normalize_facts(raw: Any, content: str) -> tuple[list[dict[str, Any]], list[str]]:
    """把模型输出规范化为台账事实；畸形条目丢弃并记 warning，不抛错。

    - 接受 {"facts": [...]} 或直接的数组；其余结构视为没有事实。
    - id 是服务端按保留顺序分配的章内序号字符串；模型给出的 id、偏移一律不采用。
    - 超长字段截断到 schemas.consistency 的上限；引句截断后仍是原引句的前缀，照常定位。
    """
    warnings: list[str] = []
    items = raw.get("facts") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return [], ["模型输出中没有事实列表"]
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    dropped = 0
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        category = _text(item.get("category"), 32).lower()
        quote = _text(item.get("quote"), QUOTE_LIMIT)
        statement = _text(item.get("statement"), STATEMENT_LIMIT) or quote[:STATEMENT_LIMIT]
        if category not in CONSISTENCY_CATEGORIES or not statement:
            dropped += 1
            continue
        subject = _text(item.get("subject"), SUBJECT_LIMIT)
        key = subject_key(subject)
        identity = (category, key, statement, quote)
        if identity in seen:
            continue
        if len(facts) >= MAX_FACTS_PER_CHAPTER:
            warnings.append(f"本章事实超过 {MAX_FACTS_PER_CHAPTER} 条，其余已忽略")
            break
        seen.add(identity)
        span = locate_quote(content, quote)
        fact = {
            "id": str(len(facts) + 1),
            "category": category,
            "subject": subject,
            "subject_key": key,
            "statement": statement,
            "time_hint": _text(item.get("time_hint"), TIME_HINT_LIMIT),
            "quote": quote,
            "start": span[0] if span else None,
            "end": span[1] if span else None,
        }
        try:
            LedgerFact.model_validate(fact)
        except ValidationError:
            dropped += 1
            continue
        facts.append(fact)
    if dropped:
        warnings.append(f"有 {dropped} 条事实格式不正确，已忽略")
    return facts, warnings
