"""模板引擎：校验规则、沙箱、渲染兜底。"""

import pytest

from app.ai.prompts.registry import TEMPLATES, sample_values
from app.ai.templating import (
    RENDER_LIMIT,
    TemplateRenderError,
    TemplateVar,
    render_messages,
    render_text,
    validate,
)

DECLS = (
    TemplateVar("content", "正文", required=True),
    TemplateVar("items", "列表", "list"),
    TemplateVar("title", "标题"),
    TemplateVar("json_rule", "规则", locked=True, required=True),
)
OK_USER = "{{ content }}\n{% for x in items %}{{ loop.index }}. {{ x }}\n{% endfor %}{{ json_rule }}"


def messages(issues):
    return [i.message for i in issues]


def test_valid_template_passes():
    assert validate(DECLS, "系统", OK_USER) == []


def test_all_default_templates_valid():
    for key, tpl in TEMPLATES.items():
        assert validate(tpl.variables, tpl.system, tpl.user) == [], key
        # 用示例值能渲染
        render_messages(tpl.variables, tpl.system, tpl.user, sample_values(key))


def test_syntax_error_reports_part_and_line():
    issues = validate(DECLS, "", "{{ content }}\n{{ json_rule }}\n{% if title %}\n没有结束")
    assert len(issues) == 1
    assert issues[0].part == "user" and issues[0].line == 4 and "语法错误" in issues[0].message


def test_unknown_variable_with_line():
    issues = validate(DECLS, "第一行\n{{ secret }}", OK_USER)
    assert [(i.part, i.line, i.message) for i in issues] == [("system", 2, "未知变量：secret")]


def test_required_variable_missing():
    issues = validate(DECLS, "", "{{ json_rule }}")
    assert messages(issues) == ["缺少必需变量 {{ content }}"]


def test_locked_variable_rules():
    assert "锁定变量 {{ json_rule }} 必须保留" in messages(validate(DECLS, "", "{{ content }}"))
    twice = validate(DECLS, "{{ json_rule }}", "{{ content }}{{ json_rule }}")
    assert any("只能出现一次" in m for m in messages(twice))
    in_if = validate(DECLS, "", "{{ content }}{% if title %}{{ json_rule }}{% endif %}")
    assert any("不能放进 if / for" in m for m in messages(in_if))
    filtered = validate(DECLS, "", "{{ content }}{{ json_rule | lower }}")
    assert any("不能放进 if / for" in m for m in messages(filtered))
    cond = validate(DECLS, "", "{{ content }}{{ json_rule if title else '' }}")
    assert any("不能放进 if / for" in m for m in messages(cond))


def test_for_only_over_declared_lists():
    assert any(
        "for 只能遍历" in m for m in messages(validate(DECLS, "", OK_USER + "{% for c in content %}{% endfor %}"))
    )
    assert any("for 只能遍历" in m for m in messages(validate(DECLS, "", OK_USER + "{% for c in [1,2] %}{% endfor %}")))
    nested = OK_USER + "{% for a in items %}{% for b in items %}{% endfor %}{% endfor %}"
    assert any("不能嵌套" in m for m in messages(validate(DECLS, "", nested)))


def test_range_and_globals_are_disabled():
    issues = validate(DECLS, "", OK_USER + "{% for i in range(9) %}{% endfor %}")
    assert issues  # range 未声明 + 函数调用 + for 来源非法
    assert any("未知变量：range" in m for m in messages(issues))
    for name in ("lipsum", "cycler", "joiner", "namespace", "dict"):
        assert any(f"未知变量：{name}" in m for m in messages(validate(DECLS, "", OK_USER + f"{{{{ {name} }}}}")))
    with pytest.raises(TemplateRenderError):
        render_text("{{ range(10) }}", {})


@pytest.mark.parametrize(
    "src",
    [
        "{{ ''.__class__ }}",
        "{{ content.__class__.__mro__ }}",
        "{{ content['__class__'] }}",
        "{{ content.upper() }}",
        "{{ 'a' * 100000000 }}",
        "{{ 9 ** 9 ** 9 }}",
        "{% set x = content %}",
        "{% macro m() %}{% endmacro %}",
        "{% include 'x' %}",
        "{{ content | replace('', 'x') }}",
        "{{ content | attr('__class__') }}",
        "{{ '%0999999999d' % 1 }}",
        "{{ '%s' % content }}",
        "{{ self }}",
        "{% for x in items %}{{ loop.cycle(1, 2) }}{% endfor %}",
        "{{ super() }}",
    ],
)
def test_sandbox_escape_rejected(src):
    assert validate(DECLS, "", OK_USER + src)


def test_sandbox_blocks_internal_attributes_at_runtime():
    # 绕过校验直接渲染也拿不到内部属性
    with pytest.raises(TemplateRenderError):
        render_text("{{ ''.__class__.__mro__ }}", {})


@pytest.mark.parametrize("src", ["{{ '%0999999999d' % 1 }}", "{{ 'a' * 999999999 }}", "{{ 9 ** 9 ** 9 }}"])
def test_sandbox_blocks_dos_operators_at_runtime(src):
    # 旧覆盖绕过了校验也不会执行乘法 / 乘方 / 取模
    with pytest.raises(TemplateRenderError):
        render_text(src, {})


def test_render_limit_stops_inside_loop():
    src = "{% for x in items %}{{ a }}{% endfor %}"
    with pytest.raises(TemplateRenderError, match="超过"):
        render_text(src, {"items": list(range(100000)), "a": "x" * 1000})


def test_length_limit():
    issues = validate(DECLS, "x" * 20001, OK_USER)
    assert messages(issues) == ["长度超过 20000 字"]


def test_render_limit():
    with pytest.raises(TemplateRenderError, match="超过"):
        render_text("{{ a }}{{ a }}", {"a": "x" * (RENDER_LIMIT // 2 + 1)})


def test_undefined_variable_fails_render():
    with pytest.raises(TemplateRenderError):
        render_text("{{ nope }}", {})


def test_missing_locked_content_appended_at_runtime():
    result = render_messages(
        DECLS, "", "{{ content }}", {"content": "正文", "items": [], "title": "", "json_rule": "规则"}
    )
    assert result.messages == [{"role": "user", "content": "正文\n\n规则"}]
    assert result.warnings


def test_empty_system_omitted_and_literal_braces_ok():
    result = render_messages(
        DECLS,
        "  ",
        '{{ content }} {"a": 1} {{ json_rule }}',
        {"content": "x", "items": [], "title": "", "json_rule": "r"},
    )
    assert result.messages == [{"role": "user", "content": 'x {"a": 1} r'}]
