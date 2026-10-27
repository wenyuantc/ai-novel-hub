"""项目备份导入时 JSON / 字符串列内的 id 重映射。

导入时每张表建立「旧 id → 新 id」映射（RemapContext.maps）。普通外键列由 importer 按注册表的 fk 规则处理；
这里处理藏在 JSON 列或字符串列里的 id：

- chapter_plans.character_ids：角色 id 列表，对应不上的直接去掉（读取时本来也会过滤已删除的角色）。
- analysis_changes.target / before / after：按 writeback_service 实际写入的键（见 ANALYSIS_ID_KEYS）递归处理，
  对应不上的置为 null；field_key（char:{cid}:x、rel:{rid}:x、relpair:{low}:{high}、career:{cid}:{career_id}、
  org:{org_id}:{cid}、fs:{fid}:x）按同样的映射改写，对应不上时置空串。
- chapter_analyses.foreshadow_candidates：每项的 ref_id 是伏笔 id。
- outline_snapshots.data：outlines / plans 的 id、plans 的 outline_id 与 character_ids、links 的 chapter_id /
  outline_id / plan_id；快照里引用的大纲 / 细纲在当前数据里已不存在时，分配一个不与现有行冲突的新 id（synthetic），
  保证快照内部自洽；links 指向已不存在的章节时去掉该条（恢复时本来也会跳过）。

对应不上的引用计入 missing，由 importer 汇总成一条警告。预览时用 identity=True 的上下文：映射就是文件内的 id 集合，
只统计 missing，不改写数值。
"""

import re
from collections import Counter
from collections.abc import Callable
from typing import Any

JsonRemap = Callable[[Any, "RemapContext"], Any]

# analysis_changes 的 target / before / after 中出现的 id 键 → 被引用的表（以 writeback_service 的写入为准）
ANALYSIS_ID_KEYS: dict[str, str] = {
    "character_id": "characters",
    "a_id": "characters",
    "b_id": "characters",
    "relationship_id": "relationships",
    "type_id": "relationship_types",
    "career_id": "careers",
    "stage_id": "career_stages",
    "organization_id": "organizations",
    "foreshadow_id": "foreshadows",
}
# 列表形式的 id 键（foreshadow_new 确认后 after 里的伏笔快照带 character_ids）
ANALYSIS_ID_LIST_KEYS: dict[str, str] = {"character_ids": "characters"}

_FIELD_KEY_RE = re.compile(r"^(char|rel|relpair|career|org|fs):(\d+):(.+)$")
# field_key 前缀 → (第一段 id 的表, 第二段是 id 时的表；None = 第二段是字段名)
_FIELD_KEY_TABLES: dict[str, tuple[str, str | None]] = {
    "char": ("characters", None),
    "rel": ("relationships", None),
    "relpair": ("characters", "characters"),
    "career": ("characters", "careers"),
    "org": ("organizations", "characters"),
    "fs": ("foreshadows", None),
}


class RemapContext:
    def __init__(self, maps: dict[str, dict[int, int]], *, identity: bool = False) -> None:
        self.maps = maps
        self.identity = identity
        self.missing: Counter[str] = Counter()
        self._synthetic: dict[tuple[str, int], int] = {}
        self.synthetic_base: dict[str, int] = {}

    def get(self, table: str, old: Any) -> int | None:
        if not isinstance(old, int) or isinstance(old, bool):
            return None
        return self.maps.get(table, {}).get(old)

    def ref(self, table: str, old: Any, where: str) -> int | None:
        """映射一个引用；对应不上时计入 missing 并返回 None（old 为 None 时原样返回 None，不计数）。"""
        if old is None:
            return None
        new = self.get(table, old)
        if new is None:
            self.missing[where] += 1
        return new

    def synthetic(self, table: str, old: int) -> int:
        """为快照中已不存在的大纲 / 细纲分配新 id：大于导入时该表的最大 id，同一个旧 id 始终得到同一个新 id。"""
        if self.identity:
            return old
        key = (table, old)
        if key not in self._synthetic:
            base = self.synthetic_base.get(table, 0) + 1
            self.synthetic_base[table] = base
            self._synthetic[key] = base
        return self._synthetic[key]


def id_list(table: str) -> JsonRemap:
    """id 列表：对应不上的去掉。"""

    def _remap(value: Any, ctx: RemapContext) -> list[int]:
        if not isinstance(value, list):
            return []
        out: list[int] = []
        for v in value:
            new = ctx.ref(table, v, f"{table} 引用")
            if new is not None:
                out.append(v if ctx.identity else new)
        return out

    return _remap


def analysis_payload(value: Any, ctx: RemapContext) -> Any:
    """analysis_changes.target / before / after：递归改写 ANALYSIS_ID_KEYS 中的键。"""
    if isinstance(value, list):
        return [analysis_payload(v, ctx) for v in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, v in value.items():
        if key in ANALYSIS_ID_KEYS and (v is None or isinstance(v, int)):
            new = ctx.ref(ANALYSIS_ID_KEYS[key], v, "分析回写条目")
            out[key] = v if ctx.identity and new is not None else new
        elif key in ANALYSIS_ID_LIST_KEYS:
            out[key] = sorted(id_list(ANALYSIS_ID_LIST_KEYS[key])(v, ctx))
        else:
            out[key] = analysis_payload(v, ctx)
    return out


def field_key(value: Any, ctx: RemapContext) -> str:
    """analysis_changes.field_key 中的 id；对应不上时置空串（不再参与「更晚章节优先」比较）。"""
    if not isinstance(value, str) or not value:
        return value if isinstance(value, str) else ""
    m = _FIELD_KEY_RE.match(value)
    if m is None:
        return value
    prefix, first, rest = m.group(1), int(m.group(2)), m.group(3)
    first_table, second_table = _FIELD_KEY_TABLES[prefix]
    new_first = ctx.ref(first_table, first, "分析回写条目")
    if new_first is None:
        return ""
    if second_table is None:
        return value if ctx.identity else f"{prefix}:{new_first}:{rest}"
    if not rest.isdigit():
        return value
    new_second = ctx.ref(second_table, int(rest), "分析回写条目")
    if new_second is None:
        return ""
    if ctx.identity:
        return value
    if prefix == "relpair":
        new_first, new_second = min(new_first, new_second), max(new_first, new_second)
    return f"{prefix}:{new_first}:{new_second}"


def foreshadow_candidates(value: Any, ctx: RemapContext) -> Any:
    """chapter_analyses.foreshadow_candidates：每项的 ref_id 是伏笔 id，对应不上置 null。"""
    if not isinstance(value, list):
        return value
    out = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("ref_id"), int):
            new = ctx.ref("foreshadows", item["ref_id"], "分析伏笔候选")
            item = {**item, "ref_id": item["ref_id"] if ctx.identity and new is not None else new}
        out.append(item)
    return out


def snapshot_data(value: Any, ctx: RemapContext) -> dict[str, Any]:
    """outline_snapshots.data：结构见 outline_snapshot_service。保证快照内部自洽（恢复时按其中的 id 重建）。"""
    if not isinstance(value, dict):
        return {"outlines": [], "plans": [], "links": []}

    def _local(table: str, old: Any) -> int | None:
        if not isinstance(old, int) or isinstance(old, bool):
            return None
        new = ctx.get(table, old)
        if new is None:
            return ctx.synthetic(table, old)
        return old if ctx.identity else new

    omap: dict[int, int] = {}
    outlines = []
    for o in value.get("outlines") or []:
        if not isinstance(o, dict):
            continue
        new = _local("outlines", o.get("id"))
        if new is None:
            continue
        omap[o["id"]] = new
        outlines.append({**o, "id": new})
    pmap: dict[int, int] = {}
    plans = []
    for p in value.get("plans") or []:
        if not isinstance(p, dict) or p.get("outline_id") not in omap:
            continue
        new = _local("chapter_plans", p.get("id"))
        if new is None:
            continue
        pmap[p["id"]] = new
        item = {**p, "id": new, "outline_id": omap[p["outline_id"]]}
        if "character_ids" in p:
            item["character_ids"] = id_list("characters")(p.get("character_ids"), ctx)
        plans.append(item)
    links = []
    for link in value.get("links") or []:
        if not isinstance(link, dict):
            continue
        chapter = ctx.get("chapters", link.get("chapter_id"))
        if chapter is None:
            # 章节已不存在：恢复时本来也会跳过，直接去掉
            continue
        links.append(
            {
                **link,
                "chapter_id": link["chapter_id"] if ctx.identity else chapter,
                "outline_id": omap.get(link.get("outline_id")) if link.get("outline_id") is not None else None,
                "plan_id": pmap.get(link.get("plan_id")) if link.get("plan_id") is not None else None,
            }
        )
    return {**value, "outlines": outlines, "plans": plans, "links": links}
