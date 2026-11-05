from app.schemas.mentions import EntityRef
from app.services.mentions.matcher import MentionCache, compile_dictionary, iter_matches, span_out


def entity(id, name, aliases=(), *, kind="character", category="character"):
    return EntityRef(kind=kind, id=id, name=name, category=category, aliases=list(aliases))


def match(text, entities):
    dictionary = compile_dictionary(entities)
    return [span_out(hit, dictionary) for hit in iter_matches(text, dictionary)]


def test_unicode_aliases_longest_and_unmodified_offsets():
    text = "\r\n  🙂林远山到青云城，林远取青锋剑。"
    spans = match(
        text,
        [
            entity(1, "林远"),
            entity(2, "林远山"),
            entity(3, "云城", ["青云城"], kind="world", category="place"),
            entity(4, "青锋剑", kind="world", category="item"),
        ],
    )
    assert [span.text for span in spans] == ["林远山", "青云城", "林远", "青锋剑"]
    assert spans[0].start == 5
    assert all(text[span.start : span.end] == span.text for span in spans)
    assert all(a.end <= b.start for a, b in zip(spans, spans[1:], strict=False))


def test_ascii_boundaries_case_and_valid_shorter_fallback():
    spans = match(
        "Anna Ann ann John AB A+B A+BC 中文Ann中文",
        [
            entity(1, "Ann"),
            entity(2, "John A"),
            entity(3, "John"),
            entity(4, "A+B"),
        ],
    )
    assert [span.text for span in spans] == ["Ann", "John", "A+B", "Ann"]


def test_same_word_candidates_are_stable_without_double_counting_aliases():
    spans = match("小白，小白", [entity(3, "小白", ["小白", " 小白 ", ""]), entity(1, "白虎", ["小白"])])
    assert len(spans) == 2
    assert [[item.id for item in span.entities] for span in spans] == [[1, 3], [1, 3]]


def test_one_character_and_earliest_nonoverlapping_match():
    assert [s.text for s in match("甲乙丙甲", [entity(1, "甲乙"), entity(2, "乙丙"), entity(3, "甲")])] == [
        "甲乙",
        "甲",
    ]
    assert match("正文", [entity(1, " ", ["", "  "])]) == []


def test_dictionary_fingerprint_tracks_identity_and_normalized_terms():
    original = compile_dictionary([entity(1, "林远", ["小林"]), entity(2, "青城", kind="world", category="place")])
    reordered = compile_dictionary(
        [entity(2, "青城", kind="world", category="place"), entity(1, "林远", [" 小林 ", "小林"])]
    )
    assert original.fingerprint == reordered.fingerprint
    assert original.fingerprint != compile_dictionary([entity(1, "林远", ["阿林"])]).fingerprint
    assert (
        original.fingerprint
        != compile_dictionary(
            [entity(1, "林远", ["小林"]), entity(2, "青城", kind="world", category="item")]
        ).fingerprint
    )


def test_cache_limits_and_empty_scans():
    cache = MentionCache(max_sources=2, max_spans=3, max_source_spans=2)
    dictionary = compile_dictionary([entity(1, "甲")])
    first = list(cache.iter_matches((1, "chapter", 1), "甲甲", dictionary))
    assert len(first) == 2
    assert cache.info() == {"sources": 1, "spans": 2}
    assert list(cache.iter_matches((1, "chapter", 2), "乙", dictionary)) == []
    assert cache.info()["sources"] == 2
    assert len(list(cache.iter_matches((1, "chapter", 3), "甲甲甲甲", dictionary))) == 4
    assert cache.info()["spans"] <= 3
    assert len(list(cache.iter_matches((1, "chapter", 1), "甲", dictionary))) == 1
    cache.clear()
    assert cache.info() == {"sources": 0, "spans": 0}
