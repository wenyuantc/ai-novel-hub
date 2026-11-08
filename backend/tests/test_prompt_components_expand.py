"""组件引用的解析与展开（纯函数）：引用查找、展开、行号映射、嵌套 / 不存在 / 格式错误，
以及默认模板与内置 Skill 不含引用。"""

import pytest

from app.ai.components import ComponentRefError, expand, expand_strict, find_refs, ref_slugs
from app.ai.prompts import registry
from app.ai.prompts.registry import TEMPLATES
from app.ai.templating import TemplateVar
from app.services import prompt_service
from app.skills import load_builtin


def test_find_refs_and_slugs():
    text = "开头\n{{> ban-words }}\n中间 {{>tone}} 与 {{>  ban-words  }}\n"
    assert find_refs(text) == [("ban-words", 2), ("tone", 3), ("ban-words", 3)]
    assert ref_slugs(text) == ["ban-words", "tone"]
    assert find_refs("没有引用 {{ x }}") == []


def test_expand_without_refs_is_identity():
    text = "原样文本 {{ var }}\n{% if x %}y{% endif %}"
    result = expand(text, {"a": "不会用到"})
    assert result.text == text and not result.pieces and not result.errors
    assert result.map_line(2) == (2, None)


def test_expand_replaces_in_place_and_maps_lines():
    text = "第一行\n{{> a }}\n第三行 {{> b }} 尾\n第四行"
    result = expand(text, {"a": "A1\nA2\nA3", "b": "B"})
    assert result.text == "第一行\nA1\nA2\nA3\n第三行 B 尾\n第四行"
    assert result.used == ["a", "b"] and not result.errors
    assert result.map_line(1) == (1, None)
    # 组件 a 占展开结果第 2–4 行 → 原文第 2 行，来自组件 a
    assert [result.map_line(n) for n in (2, 3, 4)] == [(2, "a")] * 3
    assert result.map_line(5) == (3, "b")
    assert result.map_line(6) == (4, None)


def test_expand_unknown_nested_and_bad_format():
    result = expand("x {{> nope }}\n{{> inner }}\n{{> Bad_Slug }}", {"inner": "含 {{> other }}"})
    messages = [(line, msg) for line, _, msg in result.errors]
    assert (1, "引用的组件不存在：nope") in messages
    assert (2, "组件 inner：组件内不能引用其他组件") in messages
    assert any(line == 3 and "格式不正确" in msg for line, msg in messages)
    with pytest.raises(ComponentRefError) as exc:
        expand_strict("{{> nope }}", {})
    assert exc.value.slug == "nope" and exc.value.line == 1


def test_check_maps_issue_to_component_source():
    tpl = registry.PromptTemplate(
        key="t", name="测试", category="chapter", description="", system="", user="{{ a }}",
        variables=(TemplateVar("a", "A"),),
    )
    issues, _, user = prompt_service.check(tpl, "", "开头 {{ a }}\n{{> comp }}\n结尾", {"comp": "行1\n{{ zzz }}"})
    assert user == "开头 {{ a }}\n行1\n{{ zzz }}\n结尾"
    assert [(i.part, i.line, i.message) for i in issues] == [("user", 2, "组件 comp：未知变量：zzz")]
    issues, _, _ = prompt_service.check(tpl, "", "{{ a }}\n\n{{ yyy }}", {})
    assert [(i.line, i.message) for i in issues] == [(3, "未知变量：yyy")]


def test_defaults_and_builtin_skills_have_no_refs():
    for tpl in TEMPLATES.values():
        assert "{{>" not in tpl.system and "{{>" not in tpl.user, tpl.key
    for item in load_builtin():
        assert "{{>" not in item["body"], item["builtin_key"]


def test_expand_lenient_keeps_unresolved_refs_literal():
    """Skill 运行时：能解析的展开，解析不到 / 格式不对的原样保留（兼容旧正文）。"""
    from app.ai.components import expand_lenient

    text = "A {{> ban }} B {{> nope }} C {{> Bad }}"
    assert expand_lenient(text, {"ban": "禁用词"}) == "A 禁用词 B {{> nope }} C {{> Bad }}"
    assert expand_lenient("无引用", {}) == "无引用"
