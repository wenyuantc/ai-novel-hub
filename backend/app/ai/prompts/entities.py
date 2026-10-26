"""AI 单体生成（角色 / 组织）与旧关系备注解析的提示词（模板 entity.* / relations.parse）。均要求 JSON 输出。"""

from app.ai.prompts.common import JSON_ONLY_RULE, ProjectInfo, format_project_info
from app.ai.prompts.formats import (
    CAREER_ASSIGN_EXAMPLE,
    ORGANIZATION_FORMAT,
    RELATIONS_PARSE_FORMAT,
    character_generate_format,
)
from app.ai.prompts.registry import render
from app.ai.providers.base import ChatMessage
from app.ai.templating import PromptOverride

__all__ = [
    "CAREER_ASSIGN_EXAMPLE",
    "build_character_generate_messages",
    "build_organization_generate_messages",
    "build_relations_parse_messages",
]


def build_character_generate_messages(
    info: ProjectInfo,
    *,
    world_text: str = "",
    roster_text: str = "",
    organizations_text: str = "",
    role_type: str = "",
    instruction: str = "",
    careers_text: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    careers = careers_text.strip()
    return render(
        "entity.character",
        override,
        {
            "project_info": format_project_info(info),
            "role_type": role_type.strip(),
            "world": world_text.strip(),
            "roster": roster_text.strip(),
            "organizations": organizations_text.strip(),
            "careers": careers,
            "instruction": instruction.strip(),
            "json_rule": JSON_ONLY_RULE,
            "output_format": character_generate_format(bool(careers)),
        },
    )


def build_organization_generate_messages(
    info: ProjectInfo,
    *,
    world_text: str = "",
    roster_text: str = "",
    organizations_text: str = "",
    instruction: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    return render(
        "entity.organization",
        override,
        {
            "project_info": format_project_info(info),
            "world": world_text.strip(),
            "roster": roster_text.strip(),
            "organizations": organizations_text.strip(),
            "instruction": instruction.strip(),
            "json_rule": JSON_ONLY_RULE,
            "output_format": ORGANIZATION_FORMAT,
        },
    )


def build_relations_parse_messages(
    info: ProjectInfo,
    *,
    character_name: str,
    relations_text: str,
    roster_text: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    return render(
        "relations.parse",
        override,
        {
            "project_info": format_project_info(info),
            "character_name": character_name,
            "relations_text": relations_text.strip(),
            "roster": roster_text.strip(),
            "json_rule": JSON_ONLY_RULE,
            "output_format": RELATIONS_PARSE_FORMAT,
        },
    )
