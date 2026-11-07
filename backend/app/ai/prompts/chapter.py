"""章节生成 / 润色 / 按建议重写 / 自动续写的提示词：拼好变量后交给模板渲染（模板见 templates/chapter.*.j2）。"""

from app.ai.prompts.registry import render
from app.ai.providers.base import ChatMessage
from app.ai.templating import PromptOverride


def chapter_heading(chapter_no: int, title: str) -> str:
    return f"第{chapter_no}章 {title}".rstrip()


def build_chapter_messages(
    *,
    context: str,
    chapter_no: int,
    title: str,
    summary: str,
    target_words: int,
    style: str = "",
    instruction: str = "",
    key_events: list[str] | None = None,
    goal: str = "",
    emotion: str = "",
    skill_guide: str = "",
    planning_context: str = "",
    scene_context: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    """key_events / goal / emotion 来自细纲（没有细纲时为空，提示词与之前一致）。"""
    return render(
        "chapter.generate",
        override,
        {
            "context": context,
            "chapter_no": chapter_no,
            "title": title,
            "heading": chapter_heading(chapter_no, title),
            "summary": summary.strip(),
            "key_events": [e.strip() for e in key_events or [] if e.strip()],
            "goal": goal.strip(),
            "emotion": emotion.strip(),
            "target_words": target_words,
            "style": style.strip(),
            "instruction": instruction.strip(),
            "skill_guide": skill_guide.strip(),
            "planning_context": f"\n\n{planning_context.strip()}" if planning_context.strip() else "",
            "scene_context": f"\n\n{scene_context.strip()}" if scene_context.strip() else "",
        },
    )


def build_polish_messages(
    *,
    content: str,
    style: str = "",
    instruction: str = "",
    skill_guide: str = "",
    scene_context: str = "",
    planning_context: str = "",
    setting_context: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    return render(
        "chapter.polish",
        override,
        {
            "content": content,
            "style": style.strip(),
            "instruction": instruction.strip(),
            "skill_guide": skill_guide.strip(),
            "scene_context": f"\n\n{scene_context.strip()}" if scene_context.strip() else "",
            "planning_context": f"\n\n{planning_context.strip()}" if planning_context.strip() else "",
            "setting_context": f"\n\n{setting_context.strip()}" if setting_context.strip() else "",
        },
    )


def build_revise_messages(
    *,
    context: str,
    chapter_no: int,
    title: str,
    content: str,
    suggestions: list[str],
    target_words: int,
    style: str = "",
    instruction: str = "",
    preserve: list[str] | None = None,
    skill_guide: str = "",
    planning_context: str = "",
    scene_context: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    """按分析建议整章重写：参考资料 + 原文全文 + 勾选的建议 + 补充要求 + 需保留的要素。"""
    return render(
        "chapter.revise",
        override,
        {
            "context": context,
            "chapter_no": chapter_no,
            "title": title,
            "heading": chapter_heading(chapter_no, title),
            "content": content,
            "suggestions": list(suggestions),
            "preserve": [p.strip() for p in (preserve or []) if p.strip()],
            "target_words": target_words,
            "style": style.strip(),
            "instruction": instruction.strip(),
            "skill_guide": skill_guide.strip(),
            "planning_context": f"\n\n{planning_context.strip()}" if planning_context.strip() else "",
            "scene_context": f"\n\n{scene_context.strip()}" if scene_context.strip() else "",
        },
    )


def build_continuation_messages(
    *,
    messages: list[ChatMessage],
    generated: str,
    current_words: int,
    target_words: int,
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    """整章生成字数不足时的续写：在原对话后追加已生成正文（assistant）与续写指令（user）。

    chapter.continue 只取渲染结果的 user 段（覆盖里写了 system 段也忽略）。
    """
    remaining = max(target_words - current_words, 300)
    rendered = render(
        "chapter.continue",
        override,
        {"current_words": current_words, "target_words": target_words, "remaining": remaining},
    )
    return [
        *messages,
        {"role": "assistant", "content": generated},
        {"role": "user", "content": rendered[-1]["content"]},
    ]
