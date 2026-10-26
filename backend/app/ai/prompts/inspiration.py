"""灵感模式各步骤生成选项的提示词（inspiration.<step>）。数据块由 inspiration_service 拼好后传入。"""

from app.ai.prompts.common import JSON_ONLY_RULE
from app.ai.prompts.formats import INSPIRATION_FORMAT
from app.ai.prompts.registry import render
from app.ai.providers.base import ChatMessage
from app.ai.templating import PromptOverride


def build_inspiration_messages(
    step: str,
    *,
    idea: str,
    notes: list[str],
    decided: str,
    history: str,
    feedback: str,
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    return render(
        f"inspiration.{step}",
        override,
        {
            "idea": idea.strip(),
            "notes": "\n".join(f"- {n.strip()}" for n in notes if n.strip()),
            "decided": decided.strip(),
            "history": history.strip(),
            "feedback": feedback.strip(),
            "json_rule": JSON_ONLY_RULE,
            "output_format": INSPIRATION_FORMAT,
        },
    )
