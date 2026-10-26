"""写作风格相关提示词：风格段（style.block）、从样文提取风格（style.extract）、风格试写（style.trial）。"""

from typing import Any

from app.ai.prompts.common import JSON_ONLY_RULE
from app.ai.prompts.formats import STYLE_EXTRACT_FORMAT
from app.ai.prompts.registry import render
from app.ai.providers.base import ChatMessage
from app.ai.templating import PromptOverride

BLOCK_FIELDS = (
    "name",
    "description",
    "pov",
    "rhythm",
    "dialogue",
    "description_density",
    "tone",
    "diction",
    "taboos",
    "sample",
    "extra",
)


def build_style_block(fields: dict[str, Any], override: PromptOverride | None = None) -> str:
    """把风格字段渲染成风格段（只取 user 段，去掉首尾空白）。默认模板在只有 extra 时原样输出 extra。"""
    values = {name: str(fields.get(name) or "").strip() for name in BLOCK_FIELDS}
    messages = render("style.block", override, values)
    return messages[-1]["content"].strip()


def build_style_extract_messages(*, sample_text: str, override: PromptOverride | None = None) -> list[ChatMessage]:
    return render(
        "style.extract",
        override,
        {"sample_text": sample_text.strip(), "json_rule": JSON_ONLY_RULE, "output_format": STYLE_EXTRACT_FORMAT},
    )


def build_style_trial_messages(
    *, style: str, scene: str, words: int, override: PromptOverride | None = None
) -> list[ChatMessage]:
    return render("style.trial", override, {"style": style.strip(), "scene": scene.strip(), "words": words})
