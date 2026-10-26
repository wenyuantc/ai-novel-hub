"""章节分析提示词：摘要、情节点、钩子、冲突与情感、设定变化、伏笔候选、评分与改进建议（模板 analysis.chapter）。"""

from app.ai.prompts.chapter import chapter_heading
from app.ai.prompts.common import JSON_ONLY_RULE, ProjectInfo, format_project_info
from app.ai.prompts.formats import ANALYSIS_SCHEMA, ANALYSIS_SCHEMA_WITH_FORESHADOW_REFS
from app.ai.prompts.registry import render
from app.ai.providers.base import ChatMessage
from app.ai.templating import PromptOverride

# 兼容旧名
OUTPUT_SCHEMA = ANALYSIS_SCHEMA
OUTPUT_SCHEMA_WITH_FORESHADOW_REFS = ANALYSIS_SCHEMA_WITH_FORESHADOW_REFS


def build_analysis_messages(
    info: ProjectInfo,
    *,
    chapter_no: int,
    title: str,
    content: str,
    cast_text: str,
    relations_text: str = "",
    organizations_text: str = "",
    careers_text: str = "",
    foreshadows_text: str = "",
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    """foreshadows_text 为空时（项目没有可引用的伏笔）提示词与引入伏笔管理之前逐字一致。"""
    foreshadows = foreshadows_text.strip()
    return render(
        "analysis.chapter",
        override,
        {
            "project_info": format_project_info(info),
            "chapter_no": chapter_no,
            "title": title,
            "heading": chapter_heading(chapter_no, title),
            "content": content,
            "cast": cast_text.strip(),
            "relations": relations_text.strip(),
            "organizations": organizations_text.strip(),
            "careers": careers_text.strip(),
            "foreshadows": foreshadows,
            "output_schema": ANALYSIS_SCHEMA_WITH_FORESHADOW_REFS if foreshadows else ANALYSIS_SCHEMA,
            "json_rule": JSON_ONLY_RULE,
        },
    )
