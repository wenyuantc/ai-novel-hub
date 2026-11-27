"""内置短剧提示词和返回值契约。模板来自插件安装文件，作品材料始终作为用户消息传递。"""

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from app.ai.providers.base import ChatMessage
from app.ai.templating import render_text
from app.plugins.drama.styles import STYLE_DIRECTIONS, STYLES

from .output import parse_output

__all__ = ["parse_output", "render_prompt"]

_FILES = {
    "drama.rewrite": "rewrite.j2",
    "drama.extract": "extract.j2",
    "drama.storyboard": "storyboard.j2",
    "drama.prompts": "prompts.j2",
}


@lru_cache(maxsize=6)
def _template(filename: str) -> str:
    return files(__package__).joinpath(filename).read_text(encoding="utf-8").strip()


def render_prompt(kind: str, context: dict[str, Any]) -> list[ChatMessage]:
    if kind not in _FILES:
        raise ValueError("不支持的短剧任务类型")
    key = context.get("style", "3d")
    if key not in STYLES:
        raise ValueError("未知的短剧风格")
    system = render_text(
        _template(_FILES[kind]),
        {"style": f"{STYLES[key]}（{STYLE_DIRECTIONS[key]}）", "video_rules": _template("video.j2")},
    )
    user = render_text(
        _template("request.j2"),
        {"kind": kind, "material": json.dumps(context, ensure_ascii=False, separators=(",", ":"))},
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
