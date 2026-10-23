"""局部改写提示词：改写 / 扩写 / 缩写 / 去 AI 味 / 光标处续写。"""

from typing import Literal

from app.ai.prompts.common import NOVELIST_SYSTEM
from app.ai.providers.base import ChatMessage

RewriteOp = Literal["rewrite", "expand", "condense", "deslop", "continue"]

OP_LABELS: dict[str, str] = {
    "rewrite": "改写",
    "expand": "扩写",
    "condense": "缩写",
    "deslop": "去 AI 味",
    "continue": "续写",
}

_OP_RULES: dict[str, list[str]] = {
    "rewrite": [
        "改写【待处理片段】：保留原有情节、人物行为与信息点，换一种更好的表达，使语言更流畅、更有画面感。",
        "篇幅与原片段大致相当。",
    ],
    "expand": [
        "扩写【待处理片段】：保留原有情节与信息点，补充动作、对话、心理与环境细节，让场景更饱满。",
        "篇幅约为原片段的 1.5～2 倍，不要引入与前后文冲突的新情节。",
    ],
    "condense": [
        "缩写【待处理片段】：保留关键情节、人物行为与必要信息，删去冗余描写与重复表达。",
        "篇幅约为原片段的一半，读起来仍然连贯。",
    ],
    "deslop": [
        "去除【待处理片段】中的 AI 腔：删掉套话、空洞的总结与升华、堆砌的排比和比喻，"
        "以及“仿佛”“不禁”“嘴角勾起一抹弧度”“眼中闪过一丝”之类的模板化表达。",
        "改用具体的动作、细节和符合人物身份的口语，保留原有情节与信息点，篇幅与原片段大致相当。",
    ],
    "continue": [
        "从【前文】结尾处继续往下写，约 500～1000 字（作者另有要求时以作者要求为准）。",
        "不要重复或复述前文，情节自然推进，保持人物与设定一致。",
    ],
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
) -> list[ChatMessage]:
    parts = [f"请对一章小说正文做局部{OP_LABELS[op]}。"]
    if summary.strip():
        parts.append(f"本章梗概：{summary.strip()}")
    if style.strip():
        parts.append(f"写作风格：{style.strip()}")
    parts += ["", "【前文】", before or "（无）"]
    if op != "continue":
        parts += ["", "【待处理片段】", selected, "", "【后文】", after or "（无）"]
    parts += ["", "【要求】"]
    rules = [*_OP_RULES[op]]
    if op != "continue":
        rules.append("结果要与前文、后文自然衔接，因为它会原样替换【待处理片段】。")
    rules.append(
        "只输出处理后的正文本身，不要输出前文、后文、标题、引号、说明或任何解释；不要使用 Markdown 格式。"
    )
    parts += [f"{i}. {r}" for i, r in enumerate(rules, 1)]
    if instruction.strip():
        parts.append(f"作者额外要求：{instruction.strip()}")
    return [
        {"role": "system", "content": NOVELIST_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]
