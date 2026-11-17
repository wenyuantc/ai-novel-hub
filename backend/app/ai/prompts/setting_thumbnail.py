"""图片弹窗的可编辑初始提示词；只接收已按 AI 可见性投影的字段。"""

from collections.abc import Iterable

from app.ai.prompts import registry
from app.ai.templating import PromptOverride
from app.core.errors import BadRequestError

_KINDS = {"character": "角色", "world": "世界观", "organization": "组织", "career": "职业"}
_PROMPT_LIMIT = 2000


def _render(subject: str, fields: str, override: PromptOverride | None) -> str:
    messages = registry.render(registry.SETTING_THUMBNAIL_KEY, override, {"subject": subject, "fields": fields})
    return "\n\n".join(message["content"] for message in messages if message["content"].strip()).strip()


def build_thumbnail_prompt(
    kind: str, fields: Iterable[tuple[str, str]], *, override: PromptOverride | None = None,
) -> str:
    subject = _KINDS[kind]
    # 默认模板空字段时含一个换行，另预留一处换行；保持原有总预算与逐项上限。
    budget = _PROMPT_LIMIT - len(_render(subject, "", None)) - 1
    parts = []
    for label, value in fields:
        text = str(value).strip()
        if not text or budget <= len(label) + 2:
            continue
        line = f"{label}：{text[:min(400, budget - len(label) - 2)]}"
        parts.append(line)
        budget -= len(line) + 1
    prompt = _render(subject, "\n".join(parts), override)
    if not prompt or len(prompt) > _PROMPT_LIMIT:
        raise BadRequestError("设定图片提示词模板渲染后须为 1–2000 字，请检查或缩短模板和设定")
    return prompt
