"""合并各批通读结果（纯函数）。

- 角色：姓名规范化（去空白含全角空格、统一大小写）后用并查集合并——两个条目的姓名或任一别名相同即同一角色；
  显示名取出现次数最多的姓名，别名取并集（排除显示名），文本字段取最长的非空值，count 为出现批数之和。
- 组织、职业：按规范化名称去重；职业阶段按阶段名去重，保持首次出现的顺序；组织成员按姓名去重。
- 世界观：按 (category, 规范化标题) 去重，内容取最长的一条。
- 关系：双方姓名先映射到角色显示名，按无序的 (a, b) 去重，称呼与描述保留首条。
- 体积：每类最多 MAX_ITEMS 条（超出时丢弃出现次数最少的），文本字段截断到 TEXT_LIMIT 字。

条目结构（findings）：
  characters: [{name, aliases, role_type, profile, personality, appearance, count}]
  organizations: [{name, org_type, description, members: [{name, position}], count}]
  world: [{title, category, content, count}]
  careers: [{name, kind, description, stages: [{name, description}], count}]
  relations: [{a, b, role_a, role_b, category, description, count}]
"""

import re
from collections import Counter
from typing import Any

MAX_ITEMS = 300
TEXT_LIMIT = 300
MAX_ALIASES = 10
ALIAS_LIMIT = 50
KNOWN_LIMIT = 200
KNOWN_CHARS = 3000
CATEGORIES = ("characters", "organizations", "world", "careers", "relations")
CHARACTER_TEXT_FIELDS = ("profile", "personality", "appearance")

_WS_RE = re.compile(r"\s+")


def norm(name: Any) -> str:
    return _WS_RE.sub("", str(name or "")).lower()


def empty_findings() -> dict[str, list[dict[str, Any]]]:
    return {k: [] for k in CATEGORIES}


def _clip(value: Any, limit: int = TEXT_LIMIT) -> str:
    return str(value or "").strip()[:limit]


def _longest(values: list[Any]) -> str:
    return max((_clip(v) for v in values), key=len, default="")


# ---------------- 角色 ----------------


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def merge_characters(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = [c for c in items if norm(c.get("name"))]
    uf = _UnionFind(len(items))
    owner: dict[str, int] = {}
    for i, c in enumerate(items):
        for key in {norm(c["name"]), *(norm(a) for a in c.get("aliases") or [])} - {""}:
            if key in owner:
                uf.union(owner[key], i)
            else:
                owner[key] = i
    groups: dict[int, list[dict[str, Any]]] = {}
    for i, c in enumerate(items):
        groups.setdefault(uf.find(i), []).append(c)
    out: list[dict[str, Any]] = []
    for root in sorted(groups):
        members = groups[root]
        names: Counter[str] = Counter()
        for c in members:
            names[_clip(c["name"], 100)] += int(c.get("count") or 1)
        # 出现次数最多的姓名为显示名；次数相同时取最先出现的
        display = max(names, key=lambda n: names[n])
        aliases: list[str] = []
        seen = {norm(display)}
        for c in members:
            for a in [c["name"], *(c.get("aliases") or [])]:
                a = _clip(a, ALIAS_LIMIT)
                if a and norm(a) not in seen:
                    seen.add(norm(a))
                    aliases.append(a)
        roles = Counter(_clip(c.get("role_type"), 50) for c in members if _clip(c.get("role_type")))
        merged = {
            "name": display,
            "aliases": aliases[:MAX_ALIASES],
            "role_type": roles.most_common(1)[0][0] if roles else "",
            "count": sum(int(c.get("count") or 1) for c in members),
        }
        for f in CHARACTER_TEXT_FIELDS:
            merged[f] = _longest([c.get(f) for c in members])
        out.append(merged)
    return out


def restore_aliases(final: list[dict[str, Any]], merged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """汇总输出常漏掉别名：按姓名 / 别名找到代码合并结果中的同一角色，把其姓名与别名并回（不改其他字段）。

    合并组里还含有另一个最终角色的姓名时（各批结果把两人误并、汇总又分开了）视为有歧义，不补。
    """
    owner: dict[str, int] = {}
    for i, m in enumerate(merged):
        for key in [m.get("name"), *(m.get("aliases") or [])]:
            owner.setdefault(norm(key), i)
    out: list[dict[str, Any]] = []
    for c in final:
        aliases = list(c.get("aliases") or [])
        own = {norm(c.get("name")), *(norm(a) for a in aliases)} - {""}
        others = {norm(o.get("name")) for o in final if o is not c} - own
        source = next((merged[owner[k]] for k in own if k in owner), None)
        keys = [source.get("name"), *(source.get("aliases") or [])] if source else []
        if not others & {norm(k) for k in keys}:
            for a in keys:
                a = _clip(a, ALIAS_LIMIT)
                if a and norm(a) not in own and len(aliases) < MAX_ALIASES:
                    own.add(norm(a))
                    aliases.append(a)
        out.append({**c, "aliases": aliases})
    return out


def alias_map(characters: list[dict[str, Any]]) -> dict[str, str]:
    """规范化姓名 / 别名 → 显示名。"""
    out: dict[str, str] = {}
    for c in characters:
        for key in [c["name"], *(c.get("aliases") or [])]:
            out.setdefault(norm(key), c["name"])
    return out


# ---------------- 其他实体 ----------------


def _merge_by(items: list[dict[str, Any]], key) -> dict[Any, list[dict[str, Any]]]:
    groups: dict[Any, list[dict[str, Any]]] = {}
    for item in items:
        k = key(item)
        if k:
            groups.setdefault(k, []).append(item)
    return groups


def _count(items: list[dict[str, Any]]) -> int:
    return sum(int(i.get("count") or 1) for i in items)


def merge_organizations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for group in _merge_by(items, lambda o: norm(o.get("name"))).values():
        members: list[dict[str, str]] = []
        seen: set[str] = set()
        for o in group:
            for m in o.get("members") or []:
                key = norm(m.get("name"))
                if key and key not in seen:
                    seen.add(key)
                    members.append({"name": _clip(m.get("name"), 100), "position": _clip(m.get("position"), 50)})
        out.append(
            {
                "name": _clip(group[0]["name"], 100),
                "org_type": next((_clip(o.get("org_type"), 50) for o in group if o.get("org_type")), ""),
                "description": _longest([o.get("description") for o in group]),
                "members": members[:50],
                "count": _count(group),
            }
        )
    return out


def merge_world(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for group in _merge_by(items, lambda w: (w.get("category") or "other", norm(w.get("title")))).values():
        out.append(
            {
                "title": _clip(group[0]["title"], 200),
                "category": group[0].get("category") or "other",
                "content": _longest([w.get("content") for w in group]),
                "count": _count(group),
            }
        )
    return out


def merge_careers(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for group in _merge_by(items, lambda c: norm(c.get("name"))).values():
        stages: list[dict[str, str]] = []
        seen: set[str] = set()
        for c in group:
            for s in c.get("stages") or []:
                key = norm(s.get("name"))
                if key and key not in seen:
                    seen.add(key)
                    stages.append({"name": _clip(s.get("name"), 50), "description": _clip(s.get("description"))})
        out.append(
            {
                "name": _clip(group[0]["name"], 100),
                "kind": group[0].get("kind") or "main",
                "description": _longest([c.get("description") for c in group]),
                "stages": stages[:100],
                "count": _count(group),
            }
        )
    return out


def merge_relations(items: list[dict[str, Any]], names: dict[str, str]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in items:
        a = names.get(norm(r.get("a")), _clip(r.get("a"), 100))
        b = names.get(norm(r.get("b")), _clip(r.get("b"), 100))
        if not a or not b or norm(a) == norm(b):
            continue
        key = tuple(sorted((norm(a), norm(b))))
        groups.setdefault(key, []).append({**r, "a": a, "b": b})  # type: ignore[arg-type]
    out = []
    for group in groups.values():
        first = group[0]
        out.append(
            {
                "a": first["a"],
                "b": first["b"],
                "role_a": _clip(first.get("role_a"), 20),
                "role_b": _clip(first.get("role_b"), 20),
                "category": first.get("category") or "other",
                "description": _clip(first.get("description")),
                "count": _count(group),
            }
        )
    return out


# ---------------- 累积与体积控制 ----------------


def _cap(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """超过 MAX_ITEMS 时丢弃出现次数最少的（同次数保留先出现的），保持原有顺序。"""
    if len(items) <= MAX_ITEMS:
        return items
    ranked = sorted(range(len(items)), key=lambda i: (-int(items[i].get("count") or 1), i))[:MAX_ITEMS]
    return [items[i] for i in sorted(ranked)]


def accumulate(findings: dict[str, list[dict[str, Any]]], batch: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """把一批的规范化结果并入累计结果（新条目 count = 1），返回新的 findings。"""

    def fresh(key: str) -> list[dict[str, Any]]:
        return [{**item, "count": 1} for item in batch.get(key) or []]

    characters = _cap(merge_characters([*findings.get("characters", []), *fresh("characters")]))
    names = alias_map(characters)
    return {
        "characters": characters,
        "organizations": _cap(merge_organizations([*findings.get("organizations", []), *fresh("organizations")])),
        "world": _cap(merge_world([*findings.get("world", []), *fresh("world")])),
        "careers": _cap(merge_careers([*findings.get("careers", []), *fresh("careers")])),
        "relations": _cap(merge_relations([*findings.get("relations", []), *fresh("relations")], names)),
    }


def top_characters(characters: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return sorted(characters, key=lambda c: -int(c.get("count") or 1))[:limit]


def known_characters_text(characters: list[dict[str, Any]]) -> str:
    """已知角色名单：「姓名（别名1、别名2）」，按出现次数取前 200 个、总长 ≤ 3000 字。"""
    lines: list[str] = []
    total = 0
    for c in top_characters(characters, KNOWN_LIMIT):
        aliases = "、".join(c.get("aliases") or [])
        line = f"{c['name']}（{aliases}）" if aliases else c["name"]
        if total + len(line) + 1 > KNOWN_CHARS:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)
