"""提示词（中文）。build_* 为纯函数：把数据拼成变量后交给模板渲染（registry + templates/*.j2），输出 ChatMessage 列表。

每个函数都接受可选的 override（用户覆盖，None = 系统默认）。
"""

from app.ai.prompts.analysis import build_analysis_messages
from app.ai.prompts.book_import import build_book_digest_messages, build_book_synthesize_messages
from app.ai.prompts.careers import build_career_generate_messages, build_careers_wizard_messages
from app.ai.prompts.chapter import (
    build_chapter_messages,
    build_continuation_messages,
    build_polish_messages,
    build_revise_messages,
)
from app.ai.prompts.common import ProjectInfo, format_project_info
from app.ai.prompts.entities import (
    build_character_generate_messages,
    build_organization_generate_messages,
    build_relations_parse_messages,
)
from app.ai.prompts.inspiration import build_inspiration_messages
from app.ai.prompts.outline_pro import build_outline_continue_messages, build_outline_expand_messages
from app.ai.prompts.rewrite import OP_LABELS, RewriteOp, build_rewrite_messages
from app.ai.prompts.style import build_style_block, build_style_extract_messages, build_style_trial_messages
from app.ai.prompts.wizard import (
    build_characters_messages,
    build_outline_messages,
    build_wizard_continue_messages,
    build_world_messages,
)

__all__ = [
    "OP_LABELS",
    "ProjectInfo",
    "RewriteOp",
    "build_analysis_messages",
    "build_book_digest_messages",
    "build_book_synthesize_messages",
    "build_career_generate_messages",
    "build_careers_wizard_messages",
    "build_chapter_messages",
    "build_character_generate_messages",
    "build_characters_messages",
    "build_inspiration_messages",
    "build_continuation_messages",
    "build_organization_generate_messages",
    "build_outline_continue_messages",
    "build_outline_expand_messages",
    "build_outline_messages",
    "build_polish_messages",
    "build_relations_parse_messages",
    "build_revise_messages",
    "build_rewrite_messages",
    "build_style_block",
    "build_style_extract_messages",
    "build_style_trial_messages",
    "build_wizard_continue_messages",
    "build_world_messages",
    "format_project_info",
]
