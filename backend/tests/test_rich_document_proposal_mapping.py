"""整景候选的保守映射：重复文本不猜，未改区域尽量保留。"""

import pytest

from app.schemas.rich_document import SceneDocument
from app.services.rich_document_proposal_mapping import (
    ProposalMappingError,
    build_scene_proposal,
    resolve_scene_proposal,
)

SECTION = "645d44c0-cc16-4949-973b-542022da8421"
MARKER = "d4baf738-d1ca-4d51-a9cc-22fe47213105"
OTHER = "d4baf738-d1ca-4d51-a9cc-22fe47213106"


def sample():
    return SceneDocument.model_validate({
        "content": "开甲乙尾😀",
        "formatting": {"inline": [{"kind": "bold", "start": 0, "end": 5}]},
        "sections": [{"key": SECTION, "anchor": 2, "order": 0, "title": "作者区块", "content": "资料"}],
        "markers": [
            {"key": MARKER, "status": "active", "start": 0, "end": 5, "color": "yellow"},
            {"key": OTHER, "status": "active", "start": 1, "end": 3, "color": "blue"},
        ],
    })


def test_candidate_keeps_provable_partial_styles_and_markers_but_requires_author_decisions():
    old = sample()
    plan = build_scene_proposal(old, "开新尾😀")
    assert plan.unresolved_sections == [SECTION] and plan.format_loss
    assert old.content == "开甲乙尾😀" and old.sections[0].anchor == 2
    with pytest.raises(ProposalMappingError):
        resolve_scene_proposal(plan, {SECTION: 2}, confirm_format_loss=False)
    with pytest.raises(ProposalMappingError):
        resolve_scene_proposal(plan, {}, confirm_format_loss=True)
    result = resolve_scene_proposal(plan, {SECTION: 2}, confirm_format_loss=True)
    assert result.content == "开新尾😀" and result.sections[0].anchor == 2
    assert [(s.start, s.end) for s in result.formatting.inline] == [(0, 1), (2, 4)]
    first, other = result.markers
    assert (first.status, first.start, first.end) == ("active", 0, 4)
    assert (other.status, other.last_start, other.excerpt) == ("stale", 1, "甲乙")


def test_repeated_characters_do_not_justify_guessing_section_or_marker_position():
    old = SceneDocument.model_validate({
        "content": "甲甲", "sections": [{"key": SECTION, "anchor": 1, "order": 0, "title": "区块"}],
        "markers": [{"key": MARKER, "status": "active", "start": 0, "end": 1, "color": "red"}],
    })
    plan = build_scene_proposal(old, "甲")
    assert plan.unresolved_sections == [SECTION]
    result = resolve_scene_proposal(plan, {SECTION: 0}, confirm_format_loss=False)
    assert result.markers[0].status == "stale" and result.markers[0].excerpt == "甲"


def test_unchanged_document_is_not_forced_through_confirmation():
    old = sample()
    plan = build_scene_proposal(old, old.content)
    assert not plan.requires_review
    assert resolve_scene_proposal(plan, {}, confirm_format_loss=False) == old


def test_section_positions_cannot_reverse_original_order_or_split_crlf():
    old = SceneDocument.model_validate({
        "content": "甲乙丙",
        "sections": [{"key": SECTION, "anchor": 1, "order": 0, "title": "一"},
                     {"key": OTHER, "anchor": 2, "order": 0, "title": "二"}],
    })
    plan = build_scene_proposal(old, "新\r\n稿")
    assert set(plan.unresolved_sections) == {SECTION, OTHER}
    with pytest.raises(ProposalMappingError):
        resolve_scene_proposal(plan, {SECTION: 4, OTHER: 1}, confirm_format_loss=False)
    with pytest.raises(ProposalMappingError):
        resolve_scene_proposal(plan, {SECTION: 1, OTHER: 2}, confirm_format_loss=False)
    same_anchor = resolve_scene_proposal(plan, {SECTION: 1, OTHER: 1}, confirm_format_loss=False)
    assert [(item.anchor, item.order) for item in same_anchor.sections] == [(1, 0), (1, 1)]


def test_changed_middle_block_keeps_unchanged_full_paragraphs():
    content = "首段\r\n中段\r\n末段"
    old = SceneDocument.model_validate({
        "content": content,
        "formatting": {"blocks": [{"kind": "blockquote", "start": 0, "end": len(content)}]},
    })
    plan = build_scene_proposal(old, "首段\r\n新文\r\n末段")
    assert plan.format_loss
    result = resolve_scene_proposal(plan, {}, confirm_format_loss=True)
    assert [(style.kind, result.content[style.start:style.end]) for style in result.formatting.blocks] == [
        ("blockquote", "首段"), ("blockquote", "末段"),
    ]
