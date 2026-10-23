"""AI 向导：大纲 / 角色 / 世界观生成提示词。均要求 JSON 输出。"""

from app.ai.prompts.common import JSON_ONLY_RULE, NOVELIST_SYSTEM, ProjectInfo, format_project_info
from app.ai.providers.base import ChatMessage


def _reference_block(title: str, text: str) -> str:
    return f"\n\n【已有{title}（请保持一致）】\n{text}" if text.strip() else ""


def _instruction_block(instruction: str) -> str:
    return f"\n\n【作者额外要求】\n{instruction.strip()}" if instruction.strip() else ""


def build_outline_messages(
    info: ProjectInfo,
    *,
    volume_count: int,
    chapters_per_volume: int,
    world_text: str = "",
    characters_text: str = "",
    instruction: str = "",
) -> list[ChatMessage]:
    user = (
        "请为以下小说设计分卷分章大纲。\n\n"
        f"{format_project_info(info)}"
        f"{_reference_block('世界观', world_text)}"
        f"{_reference_block('角色', characters_text)}"
        f"{_instruction_block(instruction)}\n\n"
        "【要求】\n"
        f"1. 共 {volume_count} 卷，每卷约 {chapters_per_volume} 章；章节号 chapter_no 在全书范围内从 1 开始连续递增。\n"
        "2. 每卷有卷名；每章有简短章节标题（不要带“第X章”前缀）和 50~150 字的情节梗概，体现冲突、转折与伏笔。\n"
        "3. 整体节奏张弛有度，主线清晰，卷末设置高潮或悬念。\n"
        f"4. {JSON_ONLY_RULE}\n\n"
        "【输出格式】\n"
        "{\n"
        '  "volumes": [\n'
        "    {\n"
        '      "volume_no": 1,\n'
        '      "volume_title": "卷名",\n'
        '      "chapters": [\n'
        '        {"chapter_no": 1, "title": "章节标题", "summary": "本章梗概"}\n'
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}"
    )
    return [{"role": "system", "content": NOVELIST_SYSTEM}, {"role": "user", "content": user}]


def build_characters_messages(
    info: ProjectInfo,
    *,
    character_count: int,
    world_text: str = "",
    outline_text: str = "",
    instruction: str = "",
) -> list[ChatMessage]:
    user = (
        "请为以下小说设计主要角色卡。\n\n"
        f"{format_project_info(info)}"
        f"{_reference_block('世界观', world_text)}"
        f"{_reference_block('大纲', outline_text)}"
        f"{_instruction_block(instruction)}\n\n"
        "【要求】\n"
        f"1. 设计 {character_count} 个角色（不少于 3 个），至少包含主角、重要配角与反派。\n"
        "2. role_type 取值示例：主角、女主、配角、反派、导师、路人。\n"
        "3. profile 描述外貌、性格、背景、能力与动机（100~300 字）。\n"
        "4. relations_text 用自然语言描述该角色与其他角色的关系。\n"
        f"5. {JSON_ONLY_RULE}\n\n"
        "【输出格式】\n"
        "[\n"
        '  {"name": "角色名", "role_type": "主角", "profile": "人物小传", "relations_text": "与某某是师徒……"}\n'
        "]"
    )
    return [{"role": "system", "content": NOVELIST_SYSTEM}, {"role": "user", "content": user}]


def build_world_messages(
    info: ProjectInfo,
    *,
    outline_text: str = "",
    characters_text: str = "",
    instruction: str = "",
) -> list[ChatMessage]:
    user = (
        "请为以下小说构建世界观设定。\n\n"
        f"{format_project_info(info)}"
        f"{_reference_block('大纲', outline_text)}"
        f"{_reference_block('角色', characters_text)}"
        f"{_instruction_block(instruction)}\n\n"
        "【要求】\n"
        "1. 输出 5~10 个设定条目，覆盖时代背景、地理势力、力量/科技体系、社会规则、重要历史事件等与题材相关的方面。\n"
        "2. 每个条目 content 为 100~400 字，具体、自洽、可直接用于写作参考。\n"
        f"3. {JSON_ONLY_RULE}\n\n"
        "【输出格式】\n"
        "[\n"
        '  {"title": "条目名称", "content": "设定内容"}\n'
        "]"
    )
    return [{"role": "system", "content": NOVELIST_SYSTEM}, {"role": "user", "content": user}]
