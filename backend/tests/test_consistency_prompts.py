"""一致性检查的纯函数与三个提示词模板：归一化、引句定位、事实规范化、默认模板渲染。"""

from app.ai.prompts import formats
from app.ai.prompts.common import JSON_ONLY_RULE
from app.ai.prompts.consistency import (
    build_consistency_compare_messages,
    build_consistency_extract_messages,
    build_consistency_impact_messages,
)
from app.ai.prompts.registry import TEMPLATES
from app.ai.templating import validate
from app.schemas.consistency import MAX_FACTS_PER_CHAPTER, QUOTE_LIMIT, STATEMENT_LIMIT, SUBJECT_LIMIT, LedgerFact
from app.services.consistency.facts import locate_quote, normalize_facts, subject_key
from app.services.consistency.issues import identity_key


def test_subject_key_normalizes_width_case_and_whitespace():
    assert subject_key(" 林 远\t") == "林远"
    assert subject_key("ＡＢＣ　剑") == subject_key("abc剑") == "abc剑"
    assert subject_key("Excalibur") == subject_key("EXCALIBUR") == "excalibur"
    assert subject_key("") == "" and subject_key("佩剑") != subject_key("配剑")
    assert len(subject_key("长" * 300)) == SUBJECT_LIMIT


def test_locate_quote_uses_code_points_and_first_match():
    content = "😀 林远拔剑。林远拔剑。"
    assert locate_quote(content, "林远") == (2, 4)
    assert content[2:4] == "林远"
    # 重复引句取首个匹配
    assert locate_quote(content, "林远拔剑。") == (2, 7)
    assert locate_quote("甲😀乙😀丙", "乙😀丙") == (2, 5)
    # 改写过的引句、空引句都不定位，也不做模糊匹配
    assert locate_quote(content, "林远拔出了剑") is None
    assert locate_quote(content, "") is None
    assert locate_quote(content, "林远 拔剑") is None


def test_normalize_facts_assigns_ids_locates_and_drops_malformed():
    content = "😀 林远的佩剑在山门前断成两截。"
    raw = {"facts": [
        {"category": "item", "subject": " 佩 剑 ", "statement": "佩剑断了", "quote": "佩剑在山门前断成两截",
         "id": "99", "start": 0, "end": 3, "time_hint": 7},
        "不是对象",
        {"category": "weather", "subject": "天", "statement": "下雨", "quote": "雨"},
        {"category": "item", "subject": "佩剑", "statement": "", "quote": ""},
        # 与第一条完全相同：去重
        {"category": "ITEM", "subject": "佩剑", "statement": "佩剑断了", "quote": "佩剑在山门前断成两截"},
        {"category": "timeline", "subject": None, "statement": "x" * 900, "quote": "改写过的引句"},
        {"category": "character_state", "subject": "林远", "quote": "林远"},
    ]}
    facts, warnings = normalize_facts(raw, content)
    assert [fact["id"] for fact in facts] == ["1", "2", "3"]
    first, second, third = facts
    # 模型给出的 id 与偏移一律不采用
    assert first["subject"] == "佩 剑" and first["subject_key"] == "佩剑" and first["time_hint"] == "7"
    assert content[first["start"]:first["end"]] == first["quote"] and first["start"] == 5
    assert second["subject"] == "" and len(second["statement"]) == STATEMENT_LIMIT
    assert second["start"] is None and second["end"] is None
    # 没有陈述时以引句兜底
    assert third["statement"] == "林远" and (third["start"], third["end"]) == (2, 4)
    assert warnings == ["有 3 条事实格式不正确，已忽略"]
    for fact in facts:
        LedgerFact.model_validate(fact)


def test_normalize_facts_tolerates_shapes_and_limits():
    assert normalize_facts([{"category": "item", "statement": "甲", "quote": ""}], "甲")[0][0]["id"] == "1"
    for raw in (None, "文本", 3, {"facts": "x"}, {"other": []}):
        assert normalize_facts(raw, "甲") == ([], ["模型输出中没有事实列表"])
    many = [{"category": "item", "subject": "剑", "statement": f"事实{i}", "quote": "剑" * (QUOTE_LIMIT + 50)}
            for i in range(MAX_FACTS_PER_CHAPTER + 5)]
    content = "剑" * (QUOTE_LIMIT + 50)
    facts, warnings = normalize_facts(many, content)
    assert len(facts) == MAX_FACTS_PER_CHAPTER and warnings == [f"本章事实超过 {MAX_FACTS_PER_CHAPTER} 条，其余已忽略"]
    # 超长引句截断后仍是原文前缀，照常定位
    assert len(facts[0]["quote"]) == QUOTE_LIMIT and (facts[0]["start"], facts[0]["end"]) == (0, QUOTE_LIMIT)


def test_identity_key_is_order_independent_and_field_sensitive():
    base = identity_key("item", "佩剑", [12, 5])
    assert base == identity_key("item", "佩剑", {5, 12}) and len(base) == 64
    assert base != identity_key("item", "佩剑", [5, 13])
    assert base != identity_key("timeline", "佩剑", [5, 12])
    assert base != identity_key("item", "长枪", [5, 12])


def test_templates_are_registered_valid_and_locked():
    for key, schema in (("consistency.extract", formats.CONSISTENCY_EXTRACT_SCHEMA),
                        ("consistency.compare", formats.CONSISTENCY_COMPARE_SCHEMA),
                        ("consistency.impact", formats.CONSISTENCY_IMPACT_SCHEMA)):
        template = TEMPLATES[key]
        assert template.category == "analysis"
        assert validate(template.variables, template.system, template.user) == []
        assert {var.name: var.sample for var in template.locked} == {"output_schema": schema,
                                                                    "json_rule": JSON_ONLY_RULE}


def test_extract_messages_contain_only_the_chapter_text():
    messages = build_consistency_extract_messages(chapter_no=5, title="断剑", content="林远的佩剑断成两截。")
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "不推测" in messages[0]["content"]
    user = messages[1]["content"]
    assert user.startswith(
        "请从下面这一章正文中抽取事实，供之后与其他章节比对。\n\n"
        "【正文】第5章 断剑\n林远的佩剑断成两截。\n\n【事实类别】"
    )
    assert user.count(formats.CONSISTENCY_EXTRACT_SCHEMA) == 1 and user.endswith(JSON_ONLY_RULE)
    assert "逐字摘录" in user and "作品信息" not in user
    untitled = build_consistency_extract_messages(chapter_no=7, title="", content="x")[1]["content"]
    assert untitled.count("【正文】第7章\nx") == 1


def test_compare_messages_optional_sections():
    facts = "[chapter_id=5 fact_id=1] 第5章｜物品去向｜佩剑｜断成两截｜"
    bare = build_consistency_compare_messages(facts_text=facts)[1]["content"]
    assert facts in bare and "【伏笔记录】" not in bare and "【已有问题】" not in bare
    assert "foreshadow_id 填写" not in bare and "existing_issue_id 填写" not in bare
    assert bare.count(formats.CONSISTENCY_COMPARE_SCHEMA) == 1 and bare.endswith(JSON_ONLY_RULE)
    assert "潜在冲突" in bare and "5. 没有发现冲突时输出空数组，不要省略字段。\n\n" + JSON_ONLY_RULE in bare
    full = build_consistency_compare_messages(
        facts_text=facts, foreshadows_text="#3 断剑之谜｜…", existing_issues_text="#9 物品去向｜佩剑｜…｜5、12",
    )[1]["content"]
    assert "【伏笔记录】（#编号 标题｜内容｜状态｜埋设章｜回收章）\n#3 断剑之谜｜…\n" in full
    assert "【已有问题】（#编号 类别｜标题｜说明｜涉及章节编号）\n#9 物品去向｜佩剑｜…｜5、12\n" in full
    assert "6. 事实与【伏笔记录】矛盾" in full and "7. 发现的冲突与【已有问题】" in full


def test_impact_messages_contain_only_the_two_fact_groups():
    before = "[A1] 物品去向｜佩剑｜林远的佩剑完好｜"
    after = "[B1] 物品去向｜佩剑｜林远的佩剑断成两截｜"
    messages = build_consistency_impact_messages(before_text=before, after_text=after)
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "不同措辞不算变化" in messages[0]["content"]
    user = messages[1]["content"]
    assert f"【改动前独有的事实】（[编号] 类别｜主体｜陈述｜时间线索）\n{before}\n\n【改动后独有的事实】" in user
    assert f"（[编号] 类别｜主体｜陈述｜时间线索）\n{after}\n\n" + formats.CONSISTENCY_IMPACT_SCHEMA in user
    assert user.count(formats.CONSISTENCY_IMPACT_SCHEMA) == 1 and user.endswith(JSON_ONLY_RULE)
    assert "作品信息" not in user and "【正文】" not in user
    # 一侧为空（纯新增 / 纯删除）时明确标注
    only_added = build_consistency_impact_messages(before_text="", after_text=after)[1]["content"]
    assert "【改动前独有的事实】（[编号] 类别｜主体｜陈述｜时间线索）\n（无）\n\n【改动后独有的事实】" in only_added
