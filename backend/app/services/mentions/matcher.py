"""字面匹配与有界跨度缓存。调用方在线程中消费迭代器，不缓存正文或授权。"""

import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from threading import Lock
from typing import NamedTuple

from app.schemas.mentions import EntityRef, MentionSpan

MATCHER_VERSION = 1
_ASCII_WORD = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


class Match(NamedTuple):
    start: int
    end: int
    term: str


@dataclass(frozen=True)
class Dictionary:
    entities: tuple[EntityRef, ...]
    owners: dict[str, tuple[EntityRef, ...]]
    pattern: re.Pattern[str] | None
    fingerprint: str


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compile_dictionary(entities: list[EntityRef]) -> Dictionary:
    ordered = tuple(sorted(entities, key=lambda e: (e.kind, e.id)))
    owners: dict[str, list[EntityRef]] = {}
    signature = []
    for entity in ordered:
        terms = sorted({term.strip() for term in (entity.name, *entity.aliases) if term.strip()})
        signature.append((entity.kind, entity.id, entity.name, entity.category, terms))
        for term in terms:
            owners.setdefault(term, []).append(entity)
    alternatives = []
    for term in sorted(owners, key=lambda term: (-len(term), term)):
        before = r"(?<![A-Za-z0-9_])" if term[0] in _ASCII_WORD else ""
        after = r"(?![A-Za-z0-9_])" if term[-1] in _ASCII_WORD else ""
        alternatives.append(before + re.escape(term) + after)
    fingerprint = text_hash(json.dumps([MATCHER_VERSION, signature], ensure_ascii=False, separators=(",", ":")))
    return Dictionary(
        ordered,
        {term: tuple(entries) for term, entries in owners.items()},
        re.compile("|".join(alternatives)) if alternatives else None,
        fingerprint,
    )


def iter_matches(text: str, dictionary: Dictionary) -> Iterator[Match]:
    if dictionary.pattern is not None:
        for match in dictionary.pattern.finditer(text):
            yield Match(match.start(), match.end(), match.group())


def span_out(match: Match, dictionary: Dictionary) -> MentionSpan:
    return MentionSpan(start=match.start, end=match.end, text=match.term, entities=list(dictionary.owners[match.term]))


class MentionCache:
    """LRU 仅保存跨度；过大来源继续完整流式匹配，但不进入缓存。"""

    def __init__(self, *, max_sources: int = 4096, max_spans: int = 200_000, max_source_spans: int = 20_000):
        self.max_sources = max_sources
        self.max_spans = max_spans
        self.max_source_spans = min(max_source_spans, max_spans)
        self._values: OrderedDict[tuple, tuple[Match, ...]] = OrderedDict()
        self._span_count = 0
        self._lock = Lock()

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._span_count = 0

    def info(self) -> dict[str, int]:
        with self._lock:
            return {"sources": len(self._values), "spans": self._span_count}

    def iter_matches(
        self,
        scope: tuple,
        text: str,
        dictionary: Dictionary,
        *,
        content_hash: str | None = None,
    ) -> Iterator[Match]:
        key = (*scope, content_hash or text_hash(text), dictionary.fingerprint)
        with self._lock:
            cached = self._values.get(key)
            if cached is not None:
                self._values.move_to_end(key)
        if cached is not None:
            yield from cached
            return
        collected: list[Match] | None = []
        for match in iter_matches(text, dictionary):
            if collected is not None:
                if len(collected) < self.max_source_spans:
                    collected.append(match)
                else:
                    collected = None
            yield match
        if collected is None or self.max_sources <= 0:
            return
        # 只有完整消费过的来源才算已扫描；早退的分页/窗口不留下不完整缓存。
        with self._lock:
            old = self._values.pop(key, ())
            self._span_count -= len(old)
            self._values[key] = tuple(collected)
            self._span_count += len(collected)
            while len(self._values) > self.max_sources or self._span_count > self.max_spans:
                _, removed = self._values.popitem(last=False)
                self._span_count -= len(removed)


cache = MentionCache()
