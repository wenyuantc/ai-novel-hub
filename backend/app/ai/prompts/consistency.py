"""一致性检查提示词：逐章事实抽取（consistency.extract）、跨章比对（consistency.compare）
与修改影响的事实变化判定（consistency.impact）。

抽取只接收章节正文（含开启 AI 可见的作者区块），不带作品信息与结构化设定；
比对只接收事实台账、允许进入上下文的伏笔记录与已有问题摘要；
变化判定只接收同一章改动前后的两组事实陈述。
"""

from app.ai.prompts.chapter import chapter_heading
from app.ai.prompts.common import JSON_ONLY_RULE
from app.ai.prompts.formats import (
    CONSISTENCY_COMPARE_SCHEMA,
    CONSISTENCY_EXTRACT_SCHEMA,
    CONSISTENCY_IMPACT_SCHEMA,
)
from app.ai.prompts.registry import render
from app.ai.providers.base import ChatMessage
from app.ai.templating import PromptOverride

EXTRACT_KEY = "consistency.extract"
COMPARE_KEY = "consistency.compare"
IMPACT_KEY = "consistency.impact"


def build_consistency_extract_messages(
    *, chapter_no: int, title: str, content: str, override: PromptOverride | None = None
) -> list[ChatMessage]:
    return render(
        EXTRACT_KEY,
        override,
        {
            "chapter_no": chapter_no,
            "title": title,
            "heading": chapter_heading(chapter_no, title),
            "content": content,
            "output_schema": CONSISTENCY_EXTRACT_SCHEMA,
            "json_rule": JSON_ONLY_RULE,
        },
    )


def build_consistency_compare_messages(
    *,
    facts_text: str,
    foreshadows_text: str = "",
    existing_issues_text: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    return render(
        COMPARE_KEY,
        override,
        {
            "facts": facts_text.strip(),
            "foreshadows": foreshadows_text.strip(),
            "existing_issues": existing_issues_text.strip(),
            "output_schema": CONSISTENCY_COMPARE_SCHEMA,
            "json_rule": JSON_ONLY_RULE,
        },
    )


def build_consistency_impact_messages(
    *, before_text: str, after_text: str, override: PromptOverride | None = None
) -> list[ChatMessage]:
    return render(
        IMPACT_KEY,
        override,
        {
            "before_facts": before_text.strip(),
            "after_facts": after_text.strip(),
            "output_schema": CONSISTENCY_IMPACT_SCHEMA,
            "json_rule": JSON_ONLY_RULE,
        },
    )
