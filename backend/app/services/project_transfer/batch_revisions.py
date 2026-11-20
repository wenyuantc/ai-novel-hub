"""v14 可选批次历史：验证逐景快照、章景归属及导入后可撤销的基线。"""

from collections import defaultdict
from collections.abc import Callable
from uuid import UUID

from pydantic import ValidationError

from app.schemas.book_search import MAX_HITS, PreviewRequest
from app.schemas.rich_document import SceneDocument, validate_chapter_document_boundaries
from app.services.book_search_mapping import replay_replacements

REVISION_TABLES = ("revision_batches", "revision_batch_scenes")


def _same_document(left: SceneDocument, right: SceneDocument) -> bool:
    # 数据库按行号读取标记和退场身份，独立导入不会保留旧行号。
    def comparable(doc: SceneDocument) -> dict:
        value = doc.model_dump(mode="json")
        value["markers"].sort(key=lambda item: item["key"])
        value["sections"].sort(key=lambda item: item["key"])
        value["retired_section_keys"].sort()
        return value

    return comparable(left) == comparable(right)


def validate_revision_tables(
    tables: dict[str, list[dict]], documents: dict[int, SceneDocument], history: set[tuple[int, int]],
    error: Callable[[str], None],
) -> None:
    batches = tables.get("revision_batches", [])
    if not batches:
        return  # 悬空 batch_id 由共用的外键预检拒绝。
    details: dict[int, list[dict]] = defaultdict(list)
    for row in tables.get("revision_batch_scenes", []):
        details[row["batch_id"]].append(row)

    scenes = {row["id"]: row for row in tables.get("scenes", [])}
    chapter_scenes: dict[int, list[dict]] = defaultdict(list)
    for scene in sorted(scenes.values(), key=lambda item: (item.get("sort", 0), item["id"])):
        if scene.get("active", True):
            chapter_scenes[scene["chapter_id"]].append(scene)
    stored_keys: dict[str, dict[int, set[UUID]]] = {}
    for table in ("scene_sections", "scene_markers"):
        by_scene: dict[int, set[UUID]] = defaultdict(set)
        for row in tables.get(table, []):
            by_scene[row["scene_id"]].add(UUID(row["logical_key"]))
        stored_keys[table] = by_scene

    current_batches: set[int] = set()
    for batch in batches:
        try:
            if (batch.get("status") not in ("applied", "undone")
                    or (batch["status"] == "undone") != (batch.get("undone_at") is not None)):
                raise ValueError("批次状态与撤销时间不一致")
            rows = details[batch["id"]]
            slots = {row["chapter_slot"] for row in rows}
            if (not rows or not 1 <= batch["hit_count"] <= MAX_HITS
                    or len(slots) != batch["chapter_count"] or min(slots) != 1 or max(slots) != len(slots)
                    or sum(row["hit_count"] for row in rows) != batch["hit_count"]):
                raise ValueError("批次记录与明细的章数或命中数不一致")
            PreviewRequest(query=batch["query"], replacement=batch["replacement"])
            if batch["query"] == batch["replacement"]:
                raise ValueError("批次缺少有效的查找与替换文本")

            scene_ids: set[int] = set()
            # 章号和标题均可重复。批次内的稳定序号在目标删除后仍区分各章。
            identities: dict[int, tuple[int | None, int, str, bool]] = {}
            chapter_slots: dict[int, int] = {}
            originals: dict[int, dict[int, SceneDocument]] = defaultdict(dict)
            for row in rows:
                number, cid, sid = row["chapter_no_at_apply"], row.get("chapter_id"), row.get("scene_id")
                matches = row.get("matches_revision")
                if type(matches) is not bool or number < 1:
                    raise ValueError("历史章号或章节版本匹配标志无效")
                slot = row["chapter_slot"]
                identity = (cid, number, row["chapter_title"], matches)
                if (identities.setdefault(slot, identity) != identity
                        or cid is not None and chapter_slots.setdefault(cid, slot) != slot):
                    raise ValueError("同一章节的历史标签或版本条件不一致")
                if sid is not None:
                    same_chapter = scenes[sid]["chapter_id"] == cid
                    if sid in scene_ids or not (same_chapter or not matches and (sid, cid) in history):
                        raise ValueError("重复场景或场景不属于记录中的章节")
                    scene_ids.add(sid)
                before = row["before_document"]
                if not set(SceneDocument.model_fields).issubset(before):
                    raise ValueError("原富文档快照不完整")
                document = SceneDocument.model_validate(before)
                if sid is not None:
                    section_keys = {section.key for section in document.sections} | set(document.retired_section_keys)
                    if not section_keys <= stored_keys["scene_sections"][sid] or not all(
                        marker.key in stored_keys["scene_markers"][sid] for marker in document.markers
                    ):
                        raise ValueError("场景快照含有不属于该场景的区块或标记")
                offsets = row["start_offsets"]
                if not offsets or len(offsets) != row["hit_count"]:
                    raise ValueError("场景命中数与选中位置不一致")
                mapped = replay_replacements(document, batch["query"], batch["replacement"], offsets)
                if matches:
                    if (batch["status"] != "applied" or sid is None or not scenes[sid].get("active", True)
                            or not _same_document(mapped, documents[sid])):
                        raise ValueError("声明可撤销的快照重放后与当前场景不一致")
                    originals[cid][sid] = document
            for cid, before_docs in originals.items():
                if cid in current_batches:
                    raise ValueError("同一章节只有最后一次应用的修订能匹配当前版本")
                current_batches.add(cid)
                # 当前可撤销的原稿组合也须满足跨景 CRLF 接缝约束。
                validate_chapter_document_boundaries([
                    (scene.get("separator_before", ""), before_docs.get(scene["id"], documents[scene["id"]]))
                    for scene in chapter_scenes[cid]
                ])
        except ValidationError as exc:
            error(f"批量修订 #{batch['id']} 的历史无效：{exc.errors()[0]['msg']}")
        except ValueError as exc:
            error(f"批量修订 #{batch['id']} 的历史无效：{exc}")
