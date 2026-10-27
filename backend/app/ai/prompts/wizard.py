"""AI 向导：大纲 / 角色 / 世界观生成提示词（模板 wizard.*）。均要求 JSON 输出。"""

from app.ai.prompts.common import JSON_ONLY_RULE, ProjectInfo, format_project_info
from app.ai.prompts.formats import OUTLINE_FORMAT, WORLD_FORMAT, characters_wizard_format
from app.ai.prompts.registry import render
from app.ai.providers.base import ChatMessage
from app.ai.templating import PromptOverride


def _ref(text: str) -> str:
    """参考块：有内容才输出（原文不去空白，与改造前一致）。"""
    return text if text.strip() else ""


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
    knowledge: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    """existing_text 非空 = 追加模式：接在已有大纲之后，从第 start_volume_no 卷、第 start_chapter_no 章开始写。"""
    return render(
        "wizard.outline",
        override,
        {
            "project_info": format_project_info(info),
            "volume_count": volume_count,
            "chapters_per_volume": chapters_per_volume,
            "world": _ref(world_text),
            "characters": _ref(characters_text),
            "existing": existing_text.strip(),
            "start_volume_no": start_volume_no,
            "start_chapter_no": start_chapter_no,
            "instruction": instruction.strip(),
            "knowledge": knowledge.strip(),
            "json_rule": JSON_ONLY_RULE,
            "output_format": OUTLINE_FORMAT,
        },
    )


def build_characters_messages(
    info: ProjectInfo,
    *,
    character_count: int,
    world_text: str = "",
    outline_text: str = "",
    instruction: str = "",
    careers_text: str = "",
    knowledge: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    # 项目已有职业时要求为每个角色分配职业；没有职业时提示词与原来一致
    careers = _ref(careers_text)
    return render(
        "wizard.characters",
        override,
        {
            "project_info": format_project_info(info),
            "character_count": character_count,
            "world": _ref(world_text),
            "outline": _ref(outline_text),
            "careers": careers,
            "instruction": instruction.strip(),
            "knowledge": knowledge.strip(),
            "json_rule": JSON_ONLY_RULE,
            "output_format": characters_wizard_format(bool(careers)),
        },
    )


def build_world_messages(
    info: ProjectInfo,
    *,
    outline_text: str = "",
    characters_text: str = "",
    instruction: str = "",
    knowledge: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    return render(
        "wizard.world",
        override,
        {
            "project_info": format_project_info(info),
            "outline": _ref(outline_text),
            "characters": _ref(characters_text),
            "instruction": instruction.strip(),
            "knowledge": knowledge.strip(),
            "json_rule": JSON_ONLY_RULE,
            "output_format": WORLD_FORMAT,
        },
    )


def build_wizard_continue_messages(
    *,
    messages: list[ChatMessage],
    generated: str,
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    """向导生成中断后的续写：在原对话后追加已输出内容（assistant）与续写指令（user）。

    wizard.continue 只取渲染结果的 user 段（覆盖里写了 system 段也忽略）。
    """
    rendered = render("wizard.continue", override, {"generated_chars": len(generated)})
    return [
        *messages,
        {"role": "assistant", "content": generated},
        {"role": "user", "content": rendered[-1]["content"]},
    ]
