"""Skill 自定义输入项：占位解析、输入值校验与填充（纯函数，不访问数据库）。

- 正文写 `{{ 标识 }}` 引用输入值；只有已声明的标识会被替换，其余花括号文本原样保留（旧正文兼容）。
- 顺序：组件展开（`{{> 标识 }}`）→ 填充。填充是单次 `re.sub` 回调，输入值里的 `{{ }}` / `{{> }}` 不会再被解析。
- 没有输入项（values 为空）时原样返回，保证旧 Skill 的正文逐字不变。
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.core.errors import BadRequestError

NAME_PATTERN = r"[A-Za-z_一-鿿][A-Za-z0-9_一-鿿]{0,31}"
NAME_RE = re.compile(rf"^{NAME_PATTERN}$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*(" + NAME_PATTERN + r")\s*\}\}")
KINDS = ("text", "textarea", "select")
# 各类型取值上限（字数）
VALUE_LIMITS = {"text": 200, "textarea": 5000, "select": 50}
MAX_INPUTS = 20
OPTION_LIMIT = 50
MIN_OPTIONS = 2
MAX_OPTIONS = 50
LABEL_LIMIT = 30
HELP_LIMIT = 200
# 请求里 skill_inputs 的总字数上限
VALUES_TOTAL_LIMIT = 60000


def placeholders(text: str) -> set[str]:
    """正文中形如 `{{ 标识 }}` 的占位标识（不含组件引用 `{{> x }}`）。"""
    if not text or "{{" not in text:
        return set()
    return {m.group(1) for m in PLACEHOLDER_RE.finditer(text)}


def fill(text: str, values: Mapping[str, str]) -> str:
    """只替换 values 中出现的标识；单次替换，替换进来的值不再被解析。"""
    if not values or not text or "{{" not in text:
        return text
    return PLACEHOLDER_RE.sub(lambda m: values[m.group(1)] if m.group(1) in values else m.group(0), text)


def warnings(defs: Sequence[Mapping[str, Any]], text: str) -> list[str]:
    """保存时的提示：正文里形如占位却未声明、声明了却从未引用。不阻止保存。"""
    declared = [str(d.get("name")) for d in defs]
    found = placeholders(text)
    result = [f"正文中的 {{{{ {name} }}}} 没有声明为输入项，将原样保留" for name in sorted(found - set(declared))]
    result += [f"输入项「{name}」没有在正文中引用" for name in declared if name not in found]
    return result


def check(
    defs: Sequence[Mapping[str, Any]], raw: Mapping[str, Any] | None,
) -> tuple[dict[str, str], list[str], list[str]]:
    """校验输入值并补默认值。返回 (最终值, 未填写的必填项标识, 错误说明)。

    - 未在 raw 中出现的项用默认值；出现的项按原样使用（作者可以把可选项清空）。
    - 未知标识、类型不是字符串、超长、下拉值不在选项中 → 错误；必填项去空白后为空 → 计入 missing。
    """
    raw = dict(raw or {})
    errors: list[str] = []
    missing: list[str] = []
    values: dict[str, str] = {}
    known = {str(d.get("name")) for d in defs}
    for name in raw:
        if name not in known:
            errors.append(f"未知的输入项：{str(name)[:40]}")
    for d in defs:
        name = str(d.get("name"))
        label = str(d.get("label") or name)
        kind = str(d.get("kind") or "text")
        value = raw.get(name, d.get("default") or "")
        if not isinstance(value, str):
            errors.append(f"输入项「{label}」的值应为文本")
            continue
        limit = VALUE_LIMITS.get(kind, VALUE_LIMITS["text"])
        if len(value) > limit:
            errors.append(f"输入项「{label}」不能超过 {limit} 字")
            continue
        if not value.strip():
            if d.get("required"):
                missing.append(name)
            values[name] = value
            continue
        if kind == "select" and value not in list(d.get("options") or []):
            errors.append(f"输入项「{label}」的取值不在选项中")
            continue
        values[name] = value
    return values, missing, errors


def resolve(defs: Sequence[Mapping[str, Any]], raw: Mapping[str, Any] | None) -> dict[str, str]:
    """校验 + 默认值；有任何问题 → 400（汇总全部原因）。"""
    values, missing, errors = check(defs, raw)
    labels = {str(d.get("name")): str(d.get("label") or d.get("name")) for d in defs}
    reasons = [f"必填项「{labels[name]}」未填写" for name in missing] + errors
    if reasons:
        raise BadRequestError(f"Skill 输入有误：{'；'.join(reasons)}")
    return values


def describe(defs: Sequence[Mapping[str, Any]]) -> str:
    """给智能体看的输入项清单（每行一项）。"""
    kinds = {"text": "单行文本", "textarea": "多行文本", "select": "单选"}
    lines = []
    for d in defs:
        kind = kinds.get(str(d.get("kind")), "单行文本")
        parts = [f"{{{{ {d.get('name')} }}}}", str(d.get("label") or d.get("name")), kind]
        parts.append("必填" if d.get("required") else "可选")
        if d.get("default"):
            parts.append(f"默认：{d.get('default')}")
        if d.get("options"):
            parts.append(f"选项：{' / '.join(str(o) for o in d.get('options') or [])}")
        if d.get("help"):
            parts.append(f"说明：{d.get('help')}")
        lines.append("｜".join(parts))
    return "\n".join(lines)


AGENT_FILL_INSTRUCTIONS = (
    "正文中的 {{ 名称 }} 是该 Skill 的输入占位，请根据对话内容理解并代入合适的值后再执行工作流；"
    "必填项无法从对话中确定时，先向作者追问，不要自行编造。"
)
