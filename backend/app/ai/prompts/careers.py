"""职业等级体系的提示词：AI 增量生成职业（entity.career）与向导职业步骤（wizard.careers）。均要求 JSON 输出。"""

from app.ai.prompts.common import JSON_ONLY_RULE, ProjectInfo, format_project_info
from app.ai.prompts.formats import CAREER_FORMAT
from app.ai.prompts.registry import render
from app.ai.providers.base import ChatMessage
from app.ai.templating import PromptOverride

# 兼容旧名
CAREER_OUTPUT_FORMAT = CAREER_FORMAT


def build_career_generate_messages(
    info: ProjectInfo,
    *,
    count: int,
    kind: str | None = None,
    world_text: str = "",
    careers_text: str = "",
    instruction: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    return render(
        "entity.career",
        override,
        {
            "project_info": format_project_info(info),
            "count": count,
            "kind": kind or "",
            "world": world_text.strip(),
            "careers": careers_text.strip(),
            "instruction": instruction.strip(),
            "json_rule": JSON_ONLY_RULE,
            "output_format": CAREER_FORMAT,
        },
    )


def build_careers_wizard_messages(
    info: ProjectInfo,
    *,
    career_count: int,
    world_text: str = "",
    instruction: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    return render(
        "wizard.careers",
        override,
        {
            "project_info": format_project_info(info),
            "career_count": career_count,
            "world": world_text.strip(),
            "instruction": instruction.strip(),
            "json_rule": JSON_ONLY_RULE,
            "output_format": CAREER_FORMAT,
        },
    )
