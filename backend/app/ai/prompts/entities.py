"""AI 单体生成（角色 / 组织）与旧关系备注解析的提示词。均要求 JSON 输出。"""

from app.ai.prompts.common import JSON_ONLY_RULE, NOVELIST_SYSTEM, ProjectInfo, format_project_info
from app.ai.providers.base import ChatMessage

RELATION_CATEGORY_HINT = "category 取值：family（家庭）、social（社交）、hostile（敌对）、work（职场）、other（其他）"


def _block(title: str, text: str) -> str:
    return f"\n\n【{title}】\n{text.strip()}" if text.strip() else ""


def build_character_generate_messages(
    info: ProjectInfo,
    *,
    world_text: str = "",
    roster_text: str = "",
    organizations_text: str = "",
    role_type: str = "",
    instruction: str = "",
) -> list[ChatMessage]:
    want = f"定位为「{role_type.strip()}」的" if role_type.strip() else ""
    user = (
        f"请为以下小说设计一个{want}新角色，并建议他与已有角色之间的关系。\n\n"
        f"{format_project_info(info)}"
        f"{_block('世界观', world_text)}"
        f"{_block('已有角色（名字 / 定位 / 别名）', roster_text)}"
        f"{_block('已有组织', organizations_text)}"
        f"{_block('作者额外要求', instruction)}\n\n"
        "【要求】\n"
        "1. 新角色的名字和别名不能与已有角色的名字或别名重复。\n"
        "2. role_type 取值：主角、重要配角、配角、反派、其他。\n"
        "3. appearance / personality / background 各 50~200 字，profile 为 100 字以内的一句话简介。\n"
        "4. relationships 只能指向上面列出的已有角色，target 必须使用已有角色的名字；没有合适的可以为空数组。\n"
        "   self_role 是新角色在这段关系中的身份，target_role 是对方的身份（如 self_role=徒弟、target_role=师父；"
        "对称关系两者相同，如 朋友/朋友）。\n"
        f"   {RELATION_CATEGORY_HINT}；intimacy 为 -100~100 的整数（敌对为负）。\n"
        f"5. {JSON_ONLY_RULE}\n\n"
        "【输出格式】\n"
        "{\n"
        '  "character": {"name": "角色名", "aliases": ["别名"], "role_type": "配角", "age": "18", "gender": "男",\n'
        '    "appearance": "外貌", "personality": "性格", "background": "背景", "profile": "简介"},\n'
        '  "relationships": [\n'
        '    {"target": "已有角色名", "self_role": "徒弟", "target_role": "师父", "category": "social",\n'
        '     "intimacy": 60, "description": "关系描述"}\n'
        "  ]\n"
        "}"
    )
    return [{"role": "system", "content": NOVELIST_SYSTEM}, {"role": "user", "content": user}]


def build_organization_generate_messages(
    info: ProjectInfo,
    *,
    world_text: str = "",
    roster_text: str = "",
    organizations_text: str = "",
    instruction: str = "",
) -> list[ChatMessage]:
    user = (
        "请为以下小说设计一个新的组织 / 势力，并建议哪些已有角色属于它。\n\n"
        f"{format_project_info(info)}"
        f"{_block('世界观', world_text)}"
        f"{_block('已有角色（名字 / 定位 / 别名）', roster_text)}"
        f"{_block('已有组织', organizations_text)}"
        f"{_block('作者额外要求', instruction)}\n\n"
        "【要求】\n"
        "1. 组织名称不能与已有组织重复。\n"
        "2. org_type 如宗门、帮派、王朝、公司、家族；power_level 为 0~100 的整数，表示势力强弱。\n"
        "3. parent 为上级组织名称，只能取已有组织的名称，没有则为空字符串。\n"
        "4. members 只能使用上面列出的已有角色的名字，position 为职位；没有合适的可以为空数组。\n"
        f"5. {JSON_ONLY_RULE}\n\n"
        "【输出格式】\n"
        "{\n"
        '  "organization": {"name": "组织名", "org_type": "宗门", "purpose": "宗旨", "location": "所在地",\n'
        '    "power_level": 70, "parent": "", "description": "描述"},\n'
        '  "members": [{"name": "已有角色名", "position": "长老"}]\n'
        "}"
    )
    return [{"role": "system", "content": NOVELIST_SYSTEM}, {"role": "user", "content": user}]


def build_relations_parse_messages(
    info: ProjectInfo,
    *,
    character_name: str,
    relations_text: str,
    roster_text: str = "",
) -> list[ChatMessage]:
    user = (
        f"下面是小说角色「{character_name}」的一段自然语言关系备注，请把它解析为结构化的人物关系。\n\n"
        f"{format_project_info(info)}"
        f"{_block('已有角色（名字 / 定位 / 别名）', roster_text)}"
        f"{_block(f'{character_name} 的关系备注', relations_text)}\n\n"
        "【要求】\n"
        "1. 每条关系一项；target 为对方名字，尽量使用已有角色列表中的名字，备注里没有明确对方名字的不要输出。\n"
        f"2. self_role 是「{character_name}」在这段关系中的身份，target_role 是对方的身份"
        "（如「师父是张三」→ self_role=徒弟、target_role=师父；对称关系两者相同）。\n"
        f"3. {RELATION_CATEGORY_HINT}；intimacy 为 -100~100 的整数（敌对为负）；description 简述关系。\n"
        f"4. {JSON_ONLY_RULE}\n\n"
        "【输出格式】\n"
        "{\n"
        '  "relationships": [\n'
        '    {"target": "张三", "self_role": "徒弟", "target_role": "师父", "category": "social",\n'
        '     "intimacy": 60, "description": "关系描述"}\n'
        "  ]\n"
        "}"
    )
    return [{"role": "system", "content": NOVELIST_SYSTEM}, {"role": "user", "content": user}]
