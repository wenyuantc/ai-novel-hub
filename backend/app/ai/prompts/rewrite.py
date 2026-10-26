"""局部改写提示词：改写 / 扩写 / 缩写 / 去 AI 味 / 光标处续写。

模板 templates/chapter.rewrite.*.j2，op 在模板里分支。
"""

from typing import Literal

from app.ai.prompts.registry import render
from app.ai.providers.base import ChatMessage
from app.ai.templating import PromptOverride

RewriteOp = Literal["rewrite", "expand", "condense", "deslop", "continue"]

OP_LABELS: dict[str, str] = {
    "rewrite": "改写",
    "expand": "扩写",
    "condense": "缩写",
    "deslop": "去 AI 味",
    "continue": "续写",
}


def build_rewrite_messages(
    *,
    op: RewriteOp,
    selected: str,
    before: str,
    after: str,
    style: str = "",
    summary: str = "",
    instruction: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    return render(
        "chapter.rewrite",
        override,
        {
            "op": op,
            "op_label": OP_LABELS[op],
            "selected": selected,
            "before": before or "（无）",
            "after": after or "（无）",
            "summary": summary.strip(),
            "style": style.strip(),
            "instruction": instruction.strip(),
        },
    )
