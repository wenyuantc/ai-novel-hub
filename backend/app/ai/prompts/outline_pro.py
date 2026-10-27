"""大纲进阶提示词：大纲续写（outline.continue）、单条大纲展开为多章细纲（outline.expand）。均要求 JSON 输出。"""

from app.ai.prompts.common import JSON_ONLY_RULE, ProjectInfo, format_project_info
from app.ai.prompts.formats import OUTLINE_CONTINUE_FORMAT, OUTLINE_EXPAND_FORMAT
from app.ai.prompts.registry import render
from app.ai.providers.base import ChatMessage
from app.ai.templating import PromptOverride


def build_outline_continue_messages(
    info: ProjectInfo,
    *,
    count: int,
    next_chapter_no: int,
    volume_hint: str,
    world_text: str = "",
    recent_outlines_text: str = "",
    story_so_far_text: str = "",
    foreshadows_text: str = "",
    direction: str = "",
    knowledge: str = "",
    memories: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    return render(
        "outline.continue",
        override,
        {
            "project_info": format_project_info(info),
            "count": count,
            "next_chapter_no": next_chapter_no,
            "volume_hint": volume_hint,
            "world": world_text.strip(),
            "story_so_far": story_so_far_text.strip(),
            "recent_outlines": recent_outlines_text.strip(),
            "foreshadows": foreshadows_text.strip(),
            "direction": direction.strip(),
            "knowledge": knowledge.strip(),
            "memories": memories.strip(),
            "json_rule": JSON_ONLY_RULE,
            "output_format": OUTLINE_CONTINUE_FORMAT,
        },
    )


def build_outline_expand_messages(
    info: ProjectInfo,
    *,
    outline_text: str,
    neighbors_text: str,
    roster_text: str,
    kept_text: str,
    previous_events_text: str,
    count: int,
    total: int,
    instruction: str = "",
    knowledge: str = "",
    memories: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    """kept_text：已写正文、需保留的细纲；previous_events_text：本次已生成（前几批）细纲的关键事件。"""
    return render(
        "outline.expand",
        override,
        {
            "project_info": format_project_info(info),
            "outline": outline_text.strip(),
            "neighbors": neighbors_text.strip(),
            "roster": roster_text.strip(),
            "kept": kept_text.strip(),
            "previous_events": previous_events_text.strip(),
            "count": count,
            "total": total,
            "instruction": instruction.strip(),
            "knowledge": knowledge.strip(),
            "memories": memories.strip(),
            "json_rule": JSON_ONLY_RULE,
            "output_format": OUTLINE_EXPAND_FORMAT,
        },
    )
