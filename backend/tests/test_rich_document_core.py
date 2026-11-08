"""结构化场景正文的纯合同：Unicode 坐标、作者标记与 AI 数据来源隔离。"""

import hashlib
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.core.errors import ConflictError
from app.schemas.rich_document import SceneDocument
from app.services.rich_document_conversion import conversion
from app.services.rich_document_core import (
    ai_content_pieces,
    analysis_content_fingerprint,
    selected_section_text,
)

SECTION_A = "645d44c0-cc16-4949-973b-542022da8421"
SECTION_B = "bf6068e3-562a-4e8a-afc2-9819a40e8800"
MARKER = "d4baf738-d1ca-4d51-a9cc-22fe47213105"


def document(**overrides):
    payload = {"content": "甲😀\r\n乙\n尾", "formatting": {"version": 1}, "sections": [], "markers": []}
    return SceneDocument.model_validate(payload | overrides)


def section(key=SECTION_A, **changes):
    return {
        "key": key,
        "anchor": 1,
        "order": 0,
        "title": "区块名称",
        "content": "区块内容",
        **changes,
    }


def test_plain_document_keeps_old_text_exactly_and_baseline_hash():
    value = "  甲😀\r\n\n乙e\u0301\t  \r尾  \n"
    doc = document(content=value)
    assert doc.content == value
    assert doc.formatting.inline == [] and doc.formatting.blocks == []
    assert analysis_content_fingerprint([(doc.content, doc.sections)]) == hashlib.sha256(value.encode()).hexdigest()


def test_active_marker_may_exceed_excerpt_capacity():
    doc = document(content="甲" * 5_001, markers=[{
        "key": MARKER, "status": "active", "color": "yellow", "start": 0, "end": 5_001,
    }])
    assert doc.markers[0].end == 5_001


@pytest.mark.parametrize("target", ["content", "section"])
def test_invalid_unicode_scalars_are_rejected(target):
    with pytest.raises(ValidationError, match="(?i)unicode"):
        if target == "content":
            document(content="甲\ud800乙")
        else:
            document(sections=[section(content="甲\udfff乙")])


def test_conversion_refuses_dependent_boundary_and_crlf_seam_with_surviving_anchor():
    at_boundary = document(content="甲乙", formatting={"version": 1, "boundaries": [1]}, sections=[
        section(anchor=1, content="新"), section(SECTION_B, anchor=1, order=1, content="其他"),
    ])
    crossing_crlf = document(content="\n尾", sections=[section(anchor=0, content="甲\r")], markers=[{
        "key": MARKER, "section_key": SECTION_A, "status": "active", "start": 0, "end": 2, "color": "blue",
    }])
    for doc in (at_boundary, crossing_crlf):
        with pytest.raises(ConflictError) as exc:
            conversion(doc, UUID(SECTION_A))
        assert exc.value.extra["code"] == "document_conversion_conflict"


def test_conversion_removes_unneeded_editor_boundary_and_allows_plain_crlf_seam():
    from_editor = document(content="甲乙", formatting={"version": 1, "boundaries": [1]},
                           sections=[section(anchor=1, content="备选")])
    converted = conversion(from_editor, UUID(SECTION_A))
    assert converted.content == "甲备选乙" and converted.formatting.boundaries == []
    seam = document(content="\r", sections=[section(anchor=1, content="\n尾")])
    converted = conversion(seam, UUID(SECTION_A))
    assert converted.content == "\r\n尾" and converted.formatting.boundaries == []


def test_conversion_splits_surrounding_inline_style_without_styling_inserted_text():
    doc = document(content="甲乙", formatting={"version": 1, "inline": [{"kind": "bold", "start": 0, "end": 2}]},
                   sections=[section(anchor=1, content="🙂")])
    result = conversion(doc, UUID(SECTION_A))
    assert result.content == "甲🙂乙"
    assert [(span.start, span.end) for span in result.formatting.inline] == [(0, 1), (2, 3)]


def test_conversion_keeps_section_heading_without_adding_newline_characters():
    doc = document(content="甲乙", sections=[section(anchor=1, content="题", formatting={
        "version": 1, "blocks": [{"kind": "heading", "level": 2, "start": 0, "end": 1}],
    })])
    result = conversion(doc, UUID(SECTION_A))
    assert result.content == "甲题乙" and result.formatting.boundaries == [1, 2]
    assert [(block.kind, block.level, block.start, block.end) for block in result.formatting.blocks] == [
        ("heading", 2, 1, 2),
    ]


def test_ai_projection_includes_enabled_sections_only_in_stable_location_order():
    doc = document(sections=[
        section(SECTION_A, title="隐藏名称", content="隐藏片段", anchor=1),
        section(SECTION_B, title="可用名称", content="可用片段", anchor=1, order=2, include_in_ai=True),
    ])
    pieces = ai_content_pieces(doc.content, doc.sections)
    assert [(piece.kind, piece.text) for piece in pieces] == [
        ("prose", "甲"), ("section", "可用片段"), ("prose", "😀\r\n乙\n尾"),
    ]
    assert pieces[1].section_key == UUID(SECTION_B) and pieces[1].title == "可用名称"
    assert "隐藏" not in str(pieces)
    assert doc.content == "甲😀\r\n乙\n尾"


def test_same_anchor_enabled_sections_keep_explicit_order_even_if_supplied_out_of_order():
    doc = document(sections=[
        section(SECTION_A, title="后", content="乙", anchor=1, order=8, include_in_ai=True),
        section(SECTION_B, title="前", content="甲", anchor=1, order=2, include_in_ai=True),
    ])
    pieces = ai_content_pieces(doc.content, doc.sections)
    assert [(piece.kind, piece.text) for piece in pieces] == [
        ("prose", "甲"), ("section", "甲"), ("section", "乙"), ("prose", "😀\r\n乙\n尾"),
    ]


def test_closed_section_explicit_selection_never_returns_neighbors_or_title():
    doc = document(sections=[section(content="前秘密😀目标后秘密", title="不应返回")])
    with pytest.raises(ValueError, match="确认"):
        selected_section_text(doc.sections[0], 3, 5, confirmed=False)
    with pytest.raises(ValueError, match="布尔"):
        selected_section_text(doc.sections[0], 3, 5, confirmed="yes")
    assert selected_section_text(doc.sections[0], 3, 5, confirmed=True) == "😀目"
    with pytest.raises(ValueError):
        selected_section_text(doc.sections[0], 0, 100, confirmed=True)


def test_analysis_fingerprint_changes_for_enabled_ai_material_only():
    doc = document(sections=[section(content="旧内容", include_in_ai=False)])
    baseline = analysis_content_fingerprint([(doc.content, doc.sections)])
    disabled_edit = doc.sections[0].model_copy(update={"content": "改过但仍关闭"})
    assert analysis_content_fingerprint([(doc.content, [disabled_edit])]) == baseline
    enabled = disabled_edit.model_copy(update={"include_in_ai": True})
    enabled_hash = analysis_content_fingerprint([(doc.content, [enabled])])
    assert enabled_hash != baseline
    changed = enabled.model_copy(update={"content": "新内容"})
    disabled = enabled.model_copy(update={"include_in_ai": False})
    assert analysis_content_fingerprint([(doc.content, [changed])]) != enabled_hash
    assert analysis_content_fingerprint([(doc.content, [disabled])]) == baseline


def test_active_and_stale_markers_are_different_and_target_is_scoped():
    active = {"key": MARKER, "section_key": None, "status": "active", "start": 1, "end": 2, "color": "yellow"}
    value = document(markers=[active])
    assert value.markers[0].status == "active"
    stale = active | {"status": "stale", "start": None, "end": None, "excerpt": "旧😀", "last_start": 1}
    value = document(markers=[stale])
    assert value.markers[0].excerpt == "旧😀" and value.markers[0].start is None
    with pytest.raises(ValidationError):
        document(markers=[active | {"section_key": SECTION_A}])


@pytest.mark.parametrize("changes", [
    {"sections": [section(anchor=3)]},  # CRLF 中间无可见光标。
    {"sections": [section(anchor=True)]},
    {"sections": [section(), section()]},
    {"sections": [section(), section(SECTION_B)]},  # 同一锚点不可复用顺序。
    {"sections": [section(include_in_ai="true")]},
    {"markers": [{"key": MARKER, "status": "active", "start": 3, "end": 4, "color": "yellow"}]},
    {"markers": [{"key": MARKER, "status": "active", "start": 0, "end": 100, "color": "yellow"}]},
    {"markers": [{"key": MARKER, "status": "stale", "start": 0, "end": 1, "color": "yellow"}]},
    {"formatting": {"version": 1, "inline": [{"kind": "bold", "start": 0, "end": 100}]}},
    {"formatting": {"version": 2}},
    {"formatting": {"version": 1, "unexpected": "value"}},
])
def test_invalid_structure_is_rejected_without_silent_repair(changes):
    with pytest.raises(ValidationError):
        document(**changes)


def test_paragraph_style_ranges_are_complete_and_not_ambiguous():
    text = "甲\r\n乙\n尾"
    valid = {"version": 1, "blocks": [
        {"kind": "heading", "start": 0, "end": 1, "level": 2},
        {"kind": "bullet_list", "start": 3, "end": 4},
        {"kind": "blockquote", "start": 5, "end": 6},
    ]}
    assert document(content=text, formatting=valid).formatting.blocks[0].level == 2
    # 显式零宽边界允许区块把一个自然段拆为两个有独立语义的编辑器块。
    split = {"version": 1, "boundaries": [1], "blocks": [
        {"kind": "heading", "start": 0, "end": 1, "level": 2},
        {"kind": "bullet_list", "start": 1, "end": 2},
    ]}
    assert document(content="甲乙", formatting=split).content == "甲乙"
    for invalid in [
        {"version": 1, "blocks": [{"kind": "heading", "start": 0, "end": 2, "level": 2}]},
        {"version": 1, "blocks": [{"kind": "heading", "start": 0, "end": 4, "level": 2}]},
        {"version": 1, "blocks": [
            {"kind": "heading", "start": 0, "end": 1, "level": 2},
            {"kind": "bullet_list", "start": 0, "end": 1},
        ]},
        {"version": 1, "boundaries": [2]},  # CRLF 内部。
    ]:
        with pytest.raises(ValidationError):
            document(content=text, formatting=invalid)


def test_retired_section_marker_requires_explicit_known_identity():
    stale = {"key": MARKER, "section_key": SECTION_A, "status": "stale", "start": None, "end": None,
             "last_start": 2, "excerpt": "已删片段", "color": "yellow"}
    with pytest.raises(ValidationError):
        document(markers=[stale])
    # 该集合来自服务端同景退场身份；真实 HTTP 写入还须同事务核对数据库归属。
    doc = document(markers=[stale], retired_section_keys=[SECTION_A])
    assert doc.markers[0].section_key == UUID(SECTION_A)
    with pytest.raises(ValidationError):
        document(sections=[section()], retired_section_keys=[SECTION_A])


def test_new_section_content_has_total_bound_without_limiting_old_prose():
    assert len(document(content="甲" * 2_000_001).content) == 2_000_001
    sections = [section(str(UUID(int=i + 1)), anchor=0, order=i, content="甲" * 500_000) for i in range(4)]
    document(sections=sections)
    with pytest.raises(ValidationError, match="区块正文总量"):
        document(sections=[*sections, section(str(UUID(int=5)), anchor=0, order=4, content="甲")])


def test_document_metadata_has_total_byte_bound():
    markers = [{"key": str(UUID(int=i + 1)), "status": "stale", "last_start": 0,
                "excerpt": "甲" * 4_200, "color": "yellow"} for i in range(700)]
    with pytest.raises(ValidationError, match="元数据总量"):
        document(markers=markers)
