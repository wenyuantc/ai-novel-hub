"""字面匹配与有界跨度缓存。调用方在线程中消费迭代器，不缓存正文或授权。"""

import hashlib
import json
import re
from array import array
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from heapq import merge
from itertools import groupby
from threading import Lock
from typing import NamedTuple

from app.schemas.mentions import EntityRef, MentionSpan

MATCHER_VERSION = 2
_ASCII_WORD = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
EntryKey = tuple[str, int]


class Match(NamedTuple):
    start: int
    end: int
    term: str
    owner_indices: tuple[int, ...] | None = None


@dataclass(frozen=True)
class TrackingRule:
    enabled: bool = True
    case_sensitive: bool = True
    exclusions: tuple[str, ...] = ()


class _Exclusion(NamedTuple):
    phrase: str
    offset: int
    left_boundary: bool
    right_boundary: bool


class _Owner(NamedTuple):
    index: int
    left_boundary: bool
    right_boundary: bool
    exclusions: tuple[_Exclusion, ...]


@dataclass(frozen=True)
class _Plane:
    pattern: re.Pattern[str]
    terms: dict[str, tuple[_Owner, ...]]
    lengths: dict[str, tuple[int, ...]]


@dataclass(frozen=True)
class Dictionary:
    entities: tuple[EntityRef, ...]
    enabled: frozenset[EntryKey]
    owners: dict[str, tuple[EntityRef, ...]]
    pattern: re.Pattern[str] | None
    fingerprint: str
    exact: _Plane | None = None
    folded: _Plane | None = None


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _plane(terms: dict[str, list[_Owner]]) -> _Plane | None:
    if not terms:
        return None
    lengths: dict[str, set[int]] = {}
    for term in terms:
        lengths.setdefault(term[0], set()).add(len(term))
    return _Plane(
        re.compile("|".join(re.escape(term) for term in sorted(terms, key=lambda term: (-len(term), term)))),
        {term: tuple(owners) for term, owners in terms.items()},
        {head: tuple(sorted(sizes, reverse=True)) for head, sizes in lengths.items()},
    )


def _exclusions(term: str, phrases: tuple[str, ...], *, sensitive: bool) -> tuple[_Exclusion, ...]:
    # 只有包含词面的短语才可能完整覆盖该命中。预计算相对位置，不对每条排除词重扫全文。
    found: set[_Exclusion] = set()
    for phrase in phrases:
        folded = phrase if sensitive else phrase.casefold()
        cursor = 0
        while (offset := folded.find(term, cursor)) != -1:
            found.add(_Exclusion(folded, offset, phrase[0] in _ASCII_WORD, phrase[-1] in _ASCII_WORD))
            cursor = offset + 1
    return tuple(sorted(found))


def compile_dictionary(
    entities: list[EntityRef], rules: Mapping[EntryKey, TrackingRule] | None = None,
) -> Dictionary:
    ordered = tuple(sorted(entities, key=lambda e: (e.kind, e.id)))
    owners: dict[str, list[EntityRef]] = {}
    exact: dict[str, list[_Owner]] = {}
    folded: dict[str, list[_Owner]] = {}
    enabled: set[EntryKey] = set()
    signature = []
    for index, entity in enumerate(ordered):
        terms = sorted({term.strip() for term in (entity.name, *entity.aliases) if term.strip()})
        key = (entity.kind, entity.id)
        rule = (rules or {}).get(key, TrackingRule())
        phrases = tuple(sorted({phrase.strip() for phrase in rule.exclusions if phrase.strip()}))
        signature.append((key, entity.name, entity.category, terms, rule.enabled, rule.case_sensitive, phrases))
        if not rule.enabled:
            continue
        enabled.add(key)
        for term in terms:
            owners.setdefault(term, []).append(entity)
            normalized = term if rule.case_sensitive else term.casefold()
            plane = exact if rule.case_sensitive else folded
            plane.setdefault(normalized, []).append(_Owner(
                index, term[0] in _ASCII_WORD, term[-1] in _ASCII_WORD,
                _exclusions(normalized, phrases, sensitive=rule.case_sensitive),
            ))
    fingerprint = text_hash(json.dumps([MATCHER_VERSION, signature], ensure_ascii=False, separators=(",", ":")))
    if folded or any(owner.exclusions for candidates in exact.values() for owner in candidates):
        return Dictionary(ordered, frozenset(enabled), {}, None, fingerprint, _plane(exact), _plane(folded))

    # 原大小写精确、无有效排除词场景保留 regex 非重叠快速路径（百万码点的全量扫描）。
    alternatives = []
    for term in sorted(owners, key=lambda term: (-len(term), term)):
        before = r"(?<![A-Za-z0-9_])" if term[0] in _ASCII_WORD else ""
        after = r"(?![A-Za-z0-9_])" if term[-1] in _ASCII_WORD else ""
        alternatives.append(before + re.escape(term) + after)
    return Dictionary(
        ordered,
        frozenset(enabled),
        {term: tuple(entries) for term, entries in owners.items()},
        re.compile("|".join(alternatives)) if alternatives else None,
        fingerprint,
    )


def _bounded(text: str, start: int, end: int, left: bool, right: bool) -> bool:
    return (
        not (left and start > 0 and text[start - 1] in _ASCII_WORD)
        and not (right and end < len(text) and text[end] in _ASCII_WORD)
    )


class _TextView:
    def __init__(self, text: str, *, fold: bool):
        self.text = text.casefold() if fold else text
        self.expanded_starts = array("I")
        self.expanded_ends = array("I")
        self.extra_at_end = array("I")
        if len(self.text) != len(text):
            extra = 0
            for index, char in enumerate(text):
                size = len(char.casefold())
                if size > 1:
                    self.expanded_starts.append(index + extra)
                    extra += size - 1
                    self.expanded_ends.append(index + extra + 1)
                    self.extra_at_end.append(extra)

    def original(self, pos: int) -> int | None:
        if not self.expanded_starts:
            return pos
        index = bisect_right(self.expanded_starts, pos) - 1
        if index < 0:
            return pos
        if pos >= self.expanded_ends[index]:
            return pos - self.extra_at_end[index]
        if pos == self.expanded_starts[index]:
            return pos - (self.extra_at_end[index - 1] if index > 0 else 0)
        # 不能在 ß → ss 等单个原始码点内部选择半个词面。
        return None


def _excluded(text: str, view: _TextView, start: int, owner: _Owner) -> bool:
    for phrase, offset, left, right in owner.exclusions:
        before = start - offset
        if before >= 0 and view.text.startswith(phrase, before):
            s, e = view.original(before), view.original(before + len(phrase))
            if s is not None and e is not None and _bounded(text, s, e, left, right):
                return True
    return False


def _candidates(text: str, plane: _Plane, *, fold: bool) -> Iterator[Match]:
    view = _TextView(text, fold=fold)
    cursor = 0
    while (hit := plane.pattern.search(view.text, cursor)) is not None:
        start = hit.start()
        cursor = start + 1
        s = view.original(start)
        if s is None:
            continue
        prefix = hit.group()
        for length in plane.lengths[prefix[0]]:
            if length > len(prefix):
                continue
            e = view.original(start + length)
            if e is None or not (owners := plane.terms.get(prefix[:length])):
                continue
            valid = tuple(sorted({
                owner.index for owner in owners
                if _bounded(text, s, e, owner.left_boundary, owner.right_boundary)
                and not _excluded(text, view, start, owner)
            }))
            if valid:
                yield Match(s, e, text[s:e], valid)
                break


def iter_matches(text: str, dictionary: Dictionary) -> Iterator[Match]:
    if dictionary.pattern is not None:
        for match in dictionary.pattern.finditer(text):
            yield Match(match.start(), match.end(), match.group())
        return
    streams = [
        _candidates(text, plane, fold=fold)
        for plane, fold in ((dictionary.exact, False), (dictionary.folded, True))
        if plane is not None
    ]
    cursor = 0
    for start, group in groupby(merge(*streams, key=lambda hit: hit.start), key=lambda hit: hit.start):
        if start < cursor:
            continue
        matches = tuple(group)
        cursor = max(hit.end for hit in matches)
        indices = tuple(sorted({
            index for hit in matches if hit.end == cursor for index in (hit.owner_indices or ())
        }))
        yield Match(start, cursor, text[start:cursor], indices)


def match_entities(match: Match, dictionary: Dictionary) -> tuple[EntityRef, ...]:
    if match.owner_indices is None:
        return dictionary.owners[match.term]
    return tuple(dictionary.entities[index] for index in match.owner_indices)


def span_out(match: Match, dictionary: Dictionary) -> MentionSpan:
    return MentionSpan(
        start=match.start, end=match.end, text=match.term, entities=list(match_entities(match, dictionary)),
    )


class MentionCache:
    """LRU 仅保存跨度；过大来源继续完整流式匹配，但不进入缓存。"""

    def __init__(self, *, max_sources: int = 4096, max_spans: int = 200_000, max_source_spans: int = 20_000):
        self.max_sources = max_sources
        self.max_spans = max_spans
        self.max_source_spans = min(max_source_spans, max_spans)
        self._values: OrderedDict[tuple, tuple[tuple[Match, ...], int]] = OrderedDict()
        self._span_count = 0
        self._weight = 0
        self._lock = Lock()

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._span_count = 0
            self._weight = 0

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
            yield from cached[0]
            return
        collected: list[Match] | None = []
        weight = 0
        for match in iter_matches(text, dictionary):
            if collected is not None:
                weight += 1 + len(match.owner_indices or ())
                if weight <= self.max_source_spans:
                    collected.append(match)
                else:
                    collected = None
            yield match
        if collected is None or self.max_sources <= 0:
            return
        # 只有完整消费过的来源才算已扫描；早退的分页/窗口不留下不完整缓存。
        with self._lock:
            old, old_weight = self._values.pop(key, ((), 0))
            self._span_count -= len(old)
            self._weight -= old_weight
            self._values[key] = tuple(collected), weight
            self._span_count += len(collected)
            self._weight += weight
            while len(self._values) > self.max_sources or self._weight > self.max_spans:
                _, (removed, removed_weight) = self._values.popitem(last=False)
                self._span_count -= len(removed)
                self._weight -= removed_weight


cache = MentionCache()
