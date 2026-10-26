"""提示词模板（中文）。全部为纯函数：输入纯文本参数，输出 ChatMessage 列表。"""

from app.ai.prompts.careers import build_career_generate_messages, build_careers_wizard_messages
from app.ai.prompts.chapter import build_chapter_messages, build_continuation_messages, build_polish_messages
from app.ai.prompts.common import ProjectInfo, format_project_info
from app.ai.prompts.entities import (
    build_character_generate_messages,
    build_organization_generate_messages,
    build_relations_parse_messages,
)
from app.ai.prompts.rewrite import OP_LABELS, RewriteOp, build_rewrite_messages
from app.ai.prompts.wizard import (
    build_characters_messages,
    build_outline_messages,
    build_world_messages,
)

__all__ = [
    "OP_LABELS",
    "ProjectInfo",
    "RewriteOp",
    "build_career_generate_messages",
    "build_careers_wizard_messages",
    "build_chapter_messages",
    "build_character_generate_messages",
    "build_characters_messages",
    "build_continuation_messages",
    "build_organization_generate_messages",
    "build_outline_messages",
    "build_polish_messages",
    "build_relations_parse_messages",
    "build_rewrite_messages",
    "build_world_messages",
    "format_project_info",
]
