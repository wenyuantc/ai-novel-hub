"""职业等级体系的提示词：AI 增量生成职业（career.generate）与向导职业步骤（wizard.careers）。均要求 JSON 输出。"""

from app.ai.prompts.common import JSON_ONLY_RULE, NOVELIST_SYSTEM, ProjectInfo, format_project_info
from app.ai.providers.base import ChatMessage

CAREER_KIND_HINT = (
    "kind 取值：main（主修，可作为角色的主职业）、sub（副修，只能作为副职业，如炼丹、阵法、锻造等辅助技艺）"
)

CAREER_OUTPUT_FORMAT = (
    "{\n"
    '  "careers": [\n'
    '    {"name": "剑修", "kind": "main", "description": "职业描述", "requirements": "获得条件",\n'
    '     "abilities": "特殊能力", "rules": "体系规则（升阶方式、瓶颈、限制等）",\n'
    '     "stages": [{"name": "练气", "description": "该阶段的特征"}, {"name": "筑基", "description": "..."}]}\n'
    "  ]\n"
    "}"
)


def _block(title: str, text: str) -> str:
    return f"\n\n【{title}】\n{text.strip()}" if text.strip() else ""


def _kind_requirement(kind: str | None) -> str:
    if kind == "main":
        return "全部为主修职业（kind=main）"
    if kind == "sub":
        return "全部为副修职业（kind=sub）"
    return CAREER_KIND_HINT


def build_career_generate_messages(
    info: ProjectInfo,
    *,
    count: int,
    kind: str | None = None,
    world_text: str = "",
    careers_text: str = "",
    instruction: str = "",
) -> list[ChatMessage]:
    user = (
        f"请为以下小说新设计 {count} 个职业（修炼 / 职业体系），每个职业有一条有序的等级阶梯。\n\n"
        f"{format_project_info(info)}"
        f"{_block('世界观', world_text)}"
        f"{_block('已有职业（名称 / 类别 / 阶梯，请保持体系一致）', careers_text)}"
        f"{_block('作者额外要求', instruction)}\n\n"
        "【要求】\n"
        f"1. 恰好 {count} 个职业，名称不能与已有职业重复，彼此也不能重复。\n"
        f"2. {_kind_requirement(kind)}。\n"
        "3. stages 按从低到高排列，3~12 阶，阶段名称简短（不超过 10 个字）且同一职业内不重复。\n"
        "4. description / requirements / abilities / rules 各 50~200 字，与世界观自洽。\n"
        f"5. {JSON_ONLY_RULE}\n\n"
        "【输出格式】\n"
        f"{CAREER_OUTPUT_FORMAT}"
    )
    return [{"role": "system", "content": NOVELIST_SYSTEM}, {"role": "user", "content": user}]


def build_careers_wizard_messages(
    info: ProjectInfo,
    *,
    career_count: int,
    world_text: str = "",
    instruction: str = "",
) -> list[ChatMessage]:
    user = (
        "请根据以下小说的世界观，设计整套职业（修炼 / 职业）体系，每个职业有一条有序的等级阶梯。\n\n"
        f"{format_project_info(info)}"
        f"{_block('世界观', world_text)}"
        f"{_block('作者额外要求', instruction)}\n\n"
        "【要求】\n"
        f"1. 设计约 {career_count} 个职业，至少包含 1 个主修职业；名称互不重复。\n"
        f"2. {CAREER_KIND_HINT}。\n"
        "3. stages 按从低到高排列，3~12 阶，阶段名称简短（不超过 10 个字）且同一职业内不重复。\n"
        "4. description / requirements / abilities / rules 各 50~200 字，与世界观自洽。\n"
        f"5. {JSON_ONLY_RULE}\n\n"
        "【输出格式】\n"
        f"{CAREER_OUTPUT_FORMAT}"
    )
    return [{"role": "system", "content": NOVELIST_SYSTEM}, {"role": "user", "content": user}]
