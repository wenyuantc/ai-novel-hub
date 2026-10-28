"""Skill 的 SKILL.md 格式（frontmatter + 正文）与内置 Skill。

内置 Skill 的正文是本项目自行编写的，放在 builtin/*.md；迁移 0019 与 create_all 都从这里写入。
"""

import re
from pathlib import Path
from typing import Any

import yaml

BUILTIN_DIR = Path(__file__).parent / "builtin"
BUILTIN_KEYS = ("long-continue", "deslop", "chapter-diagnose", "foreshadow-check")
_FRONT = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.S)


class SkillFormatError(ValueError):
    """SKILL.md 格式不正确（message 为可展示的中文）。"""


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """返回 (frontmatter, 正文)。正文原样保留（只把 CRLF 统一为 LF），保证导出再导入一致。"""
    text = text.replace("\r\n", "\n").lstrip("﻿")
    match = _FRONT.match(text)
    if match is None:
        raise SkillFormatError("SKILL.md 缺少 frontmatter（以 --- 开头和结尾的 YAML 段）")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillFormatError("SKILL.md 的 frontmatter 不是合法的 YAML") from exc
    if not isinstance(meta, dict):
        raise SkillFormatError("SKILL.md 的 frontmatter 必须是键值对")
    return meta, text[match.end() :]


def dump_skill_md(meta: dict[str, Any], body: str) -> str:
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{front}---\n{body}"


def builtin_fields(key: str) -> dict[str, Any]:
    """内置 Skill 的字段（不含参考资料）。"""
    meta, body = parse_skill_md((BUILTIN_DIR / f"{key}.md").read_text(encoding="utf-8"))
    return {
        "builtin_key": key,
        "slug": str(meta["name"]),
        "name": str(meta.get("display_name") or meta["name"]),
        "description": str(meta.get("description") or ""),
        "category": str(meta.get("category") or ""),
        "triggers": [str(t) for t in meta.get("triggers") or []],
        "body": body,
    }


def load_builtin() -> list[dict[str, Any]]:
    return [builtin_fields(key) for key in BUILTIN_KEYS]
