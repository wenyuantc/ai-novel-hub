"""大纲进阶提示词：大纲续写（outline.continue）、单条大纲展开为多章细纲（outline.expand）。均要求 JSON 输出。"""

from app.ai.prompts.common import JSON_ONLY_RULE, NOVELIST_SYSTEM, ProjectInfo, format_project_info
from app.ai.providers.base import ChatMessage


def _block(title: str, text: str) -> str:
    return f"\n\n【{title}】\n{text.strip()}" if text.strip() else ""


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
) -> list[ChatMessage]:
    user = (
        "请在已有大纲之后续写新的章纲。\n\n"
        f"{format_project_info(info)}"
        f"{_block('世界观', world_text)}"
        f"{_block('前文进展（每章摘要）', story_so_far_text)}"
        f"{_block('最近的章纲', recent_outlines_text)}"
        f"{_block('尚未回收的伏笔', foreshadows_text)}"
        f"{_block('作者给的发展方向', direction)}\n\n"
        "【要求】\n"
        f"1. 续写 {count} 章，从第 {next_chapter_no} 章开始，紧接最近的章纲自然推进，不要重复已发生的情节。\n"
        f"2. {volume_hint}\n"
        "3. 每章有简短章节标题（不要带“第X章”前缀）和 50~150 字的情节梗概，体现冲突、转折；"
        "适时推进或回收未回收的伏笔。\n"
        f"4. {JSON_ONLY_RULE}\n\n"
        "【输出格式】\n"
        "{\n"
        '  "volume_title": "（仅开新卷且未指定卷名时填写）",\n'
        '  "outlines": [\n'
        '    {"title": "章节标题", "summary": "本章梗概"}\n'
        "  ]\n"
        "}"
    )
    return [{"role": "system", "content": NOVELIST_SYSTEM}, {"role": "user", "content": user}]


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
) -> list[ChatMessage]:
    """kept_text：已写正文、需保留的细纲；previous_events_text：本次已生成（前几批）细纲的关键事件。"""
    user = (
        "请把下面这一条章纲展开为多章细纲。\n\n"
        f"{format_project_info(info)}"
        f"{_block('要展开的章纲', outline_text)}"
        f"{_block('前后相邻的章纲（只作衔接参考，不要写进本次细纲）', neighbors_text)}"
        f"{_block('角色名单（出场角色请使用这里的名字）', roster_text)}"
        f"{_block('已写正文、保留不变的细纲（新细纲不得重复其中的关键事件）', kept_text)}"
        f"{_block('本次已生成的细纲关键事件（不得重复）', previous_events_text)}"
        f"{_block('作者额外要求', instruction)}\n\n"
        "【要求】\n"
        f"1. 这条章纲共展开为 {total} 章；本次输出其中的 {count} 章细纲，按时间顺序排列。\n"
        "2. 每章细纲包含：标题（不要带“第X章”前缀）、100~200 字梗概、2~5 个关键事件、出场角色（名字列表）、"
        "情感基调、叙事目标（本章要达成的作用）、预估字数（2000~5000）。\n"
        "3. 多章之间的关键事件不得重复，整体完整覆盖这条章纲的情节，并与前后章纲自然衔接。\n"
        f"4. {JSON_ONLY_RULE}\n\n"
        "【输出格式】\n"
        "{\n"
        '  "chapters": [\n'
        "    {\n"
        '      "title": "细纲标题",\n'
        '      "summary": "本章梗概",\n'
        '      "key_events": ["关键事件1", "关键事件2"],\n'
        '      "characters": ["角色名"],\n'
        '      "emotion": "情感基调",\n'
        '      "goal": "叙事目标",\n'
        '      "est_words": 3000\n'
        "    }\n"
        "  ]\n"
        "}"
    )
    return [{"role": "system", "content": NOVELIST_SYSTEM}, {"role": "user", "content": user}]
