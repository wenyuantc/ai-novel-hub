"""AI 向导：大纲 / 角色 / 世界观生成提示词。均要求 JSON 输出。"""

from app.ai.prompts.common import JSON_ONLY_RULE, NOVELIST_SYSTEM, ProjectInfo, format_project_info
from app.ai.prompts.entities import CAREER_ASSIGN_EXAMPLE, CAREER_ASSIGN_RULE
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
    existing_text: str = "",
    start_volume_no: int = 1,
    start_chapter_no: int = 1,
) -> list[ChatMessage]:
    """existing_text 非空 = 追加模式：接在已有大纲之后，从第 start_volume_no 卷、第 start_chapter_no 章开始写。"""
    if existing_text.strip():
        head = "请在已有大纲之后继续设计新的分卷分章大纲。\n\n"
        rule = (
            f"1. 新写 {volume_count} 卷，每卷约 {chapters_per_volume} 章；从第 {start_volume_no} 卷、"
            f"第 {start_chapter_no} 章开始，卷号与章节号 chapter_no 接着已有大纲连续递增；"
            "情节紧接已有大纲的结尾，不要重复已有内容。\n"
        )
        existing = f"\n\n【已有大纲（末尾部分）】\n{existing_text.strip()}"
    else:
        head = "请为以下小说设计分卷分章大纲。\n\n"
        rule = (
            f"1. 共 {volume_count} 卷，每卷约 {chapters_per_volume} 章；"
            "章节号 chapter_no 在全书范围内从 1 开始连续递增。\n"
        )
        existing = ""
    user = (
        head + f"{format_project_info(info)}"
        f"{_reference_block('世界观', world_text)}"
        f"{_reference_block('角色', characters_text)}"
        f"{existing}"
        f"{_instruction_block(instruction)}\n\n"
        "【要求】\n"
        f"{rule}"
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
    careers_text: str = "",
) -> list[ChatMessage]:
    # 项目已有职业时要求为每个角色分配职业；没有职业时提示词与原来一致
    has_careers = bool(careers_text.strip())
    career_rule = f"5. {CAREER_ASSIGN_RULE}\n" if has_careers else ""
    json_no = 6 if has_careers else 5
    career_field = f',\n     "careers": {CAREER_ASSIGN_EXAMPLE}' if has_careers else ""
    user = (
        "请为以下小说设计主要角色卡，并给出角色之间的关系。\n\n"
        f"{format_project_info(info)}"
        f"{_reference_block('世界观', world_text)}"
        f"{_reference_block('大纲', outline_text)}"
        f"{_reference_block('职业体系（名称 / 类别 / 阶梯）', careers_text)}"
        f"{_instruction_block(instruction)}\n\n"
        "【要求】\n"
        f"1. 设计 {character_count} 个角色（不少于 3 个），至少包含主角、重要配角与反派。\n"
        "2. role_type 取值：主角、重要配角、配角、反派、其他。\n"
        "3. aliases 为别名 / 称号列表（可为空）；appearance / personality / background 各 50~200 字；"
        "profile 为 100 字以内的一句话简介。\n"
        "4. relationships 描述角色两两之间的关系：a、b 必须是 characters 中的名字；"
        "含义为「a 是 b 的 role_a，b 是 a 的 role_b」"
        "（如 a=张三、role_a=师父、b=李四、role_b=徒弟；对称关系两者相同，如 朋友/朋友）。\n"
        "   category 取值：family、social、hostile、work、other；intimacy 为 -100~100 的整数（敌对为负）。\n"
        f"{career_rule}"
        f"{json_no}. {JSON_ONLY_RULE}\n\n"
        "【输出格式】\n"
        "{\n"
        '  "characters": [\n'
        '    {"name": "角色名", "aliases": ["别名"], "role_type": "主角", "age": "18", "gender": "男",\n'
        '     "appearance": "外貌", "personality": "性格", "background": "背景", "profile": "简介"'
        f"{career_field}}}\n"
        "  ],\n"
        '  "relationships": [\n'
        '    {"a": "张三", "b": "李四", "role_a": "师父", "role_b": "徒弟", "category": "social",\n'
        '     "intimacy": 60, "description": "关系描述"}\n'
        "  ]\n"
        "}"
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
        "3. category 为条目分类，取值：place（地点）、faction（势力）、item（物品）、rule（规则 / 体系）、"
        "history（历史）、other（其他）。\n"
        f"4. {JSON_ONLY_RULE}\n\n"
        "【输出格式】\n"
        "[\n"
        '  {"title": "条目名称", "category": "place", "content": "设定内容"}\n'
        "]"
    )
    return [{"role": "system", "content": NOVELIST_SYSTEM}, {"role": "user", "content": user}]
