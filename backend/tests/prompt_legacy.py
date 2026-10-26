"""改造前的提示词对照实现（逐字复制自 app/ai/prompts/*.py，仅供 test_prompt_parity 使用）。

之后有意修改某个默认模板时，同步修改这里对应的函数，或删掉对应的 parity 用例（见 spec prompts-styles.md）。
"""

# ruff: noqa: E501
from dataclasses import dataclass
from typing import Literal

from app.ai.providers.base import ChatMessage

# ===== common.py =====
NOVELIST_SYSTEM = (
    "你是一位经验丰富的中文网络小说作者与编辑，擅长构建世界观、塑造立体人物、设计环环相扣的情节。"
    "你的输出始终使用简体中文。"
)

JSON_ONLY_RULE = (
    "只输出 JSON，不要输出任何解释、前言或 Markdown 代码围栏之外的文字。"
    "JSON 必须合法：使用双引号，不要尾随逗号，不要注释。"
)


@dataclass(frozen=True)
class ProjectInfo:
    title: str
    genre: str = ""
    premise: str = ""
    style: str = ""


def format_project_info(info: ProjectInfo) -> str:
    lines = [f"书名：《{info.title}》"]
    if info.genre:
        lines.append(f"题材：{info.genre}")
    if info.premise:
        lines.append(f"一句话设定：{info.premise}")
    if info.style:
        lines.append(f"写作风格：{info.style}")
    return "\n".join(lines)


# ===== chapter.py =====
PROSE_RULES = (
    "直接输出小说正文，不要输出章节标题、作者按语、总结或任何解释性文字；不要使用 Markdown 格式；段落之间用空行分隔。"
)


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
) -> list[ChatMessage]:
    """key_events / goal / emotion 来自细纲（没有细纲时为空，提示词与之前一致）。"""
    parts = [
        "以下是创作参考资料：",
        context,
        "",
        f"【本章任务】第{chapter_no}章 {title}".rstrip(),
    ]
    if summary.strip():
        parts.append(f"本章梗概：{summary.strip()}")
    events = [e.strip() for e in key_events or [] if e.strip()]
    if events:
        parts.append("关键事件：")
        parts += [f"{i}. {e}" for i, e in enumerate(events, start=1)]
    if goal.strip():
        parts.append(f"叙事目标：{goal.strip()}")
    if emotion.strip():
        parts.append(f"情感基调：{emotion.strip()}")
    parts.append(f"目标字数：约 {target_words} 字。")
    if style.strip():
        parts.append(f"写作风格：{style.strip()}")
    if instruction.strip():
        parts.append(f"作者额外要求：{instruction.strip()}")
    parts += [
        "",
        "【写作要求】",
        "1. 与前文自然衔接，保持人物性格、设定与已发生事件一致，不要重复前文内容。",
        "2. 情节推进围绕本章梗概展开，多用场景描写、动作与对话，少用概括性叙述。",
        "3. 章末留有钩子，引出后续情节。",
        f"4. {PROSE_RULES}",
    ]
    return [
        {"role": "system", "content": NOVELIST_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]


def build_polish_messages(*, content: str, style: str = "", instruction: str = "") -> list[ChatMessage]:
    parts = [
        "请润色下面这一章小说正文。",
        "【润色要求】",
        "1. 保留原有情节、人物与信息点，不删减关键剧情，不新增与原文冲突的设定。",
        "2. 优化语言流畅度、节奏与画面感，修正病句、错别字与重复表达。",
        f"3. {PROSE_RULES}",
    ]
    if style.strip():
        parts.append(f"写作风格：{style.strip()}")
    if instruction.strip():
        parts.append(f"作者额外要求：{instruction.strip()}")
    parts += ["", "【原文】", content]
    return [
        {"role": "system", "content": NOVELIST_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]


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
) -> list[ChatMessage]:
    """按分析建议整章重写：参考资料 + 原文全文 + 勾选的建议 + 补充要求 + 需保留的要素。"""
    parts = [
        "以下是创作参考资料：",
        context,
        "",
        f"【重写任务】请根据修改建议重写第{chapter_no}章 {title}".rstrip(),
    ]
    if suggestions:
        parts.append("【修改建议】")
        parts += [f"{i}. {s}" for i, s in enumerate(suggestions, start=1)]
    if instruction.strip():
        parts.append(f"【作者补充要求】{instruction.strip()}")
    kept = [p.strip() for p in (preserve or []) if p.strip()]
    if kept:
        parts.append(f"【必须保留】{'；'.join(kept)}")
    parts.append(f"目标字数：约 {target_words} 字。")
    if style.strip():
        parts.append(f"写作风格：{style.strip()}")
    parts += [
        "",
        "【重写要求】",
        "1. 逐条落实修改建议，同时保持与前文衔接，人物性格、设定与已发生事件保持一致。",
        "2. 保留原文中仍然有效的情节与信息点，不新增与设定冲突的内容。",
        f"3. {PROSE_RULES}",
        "",
        "【原文】",
        content,
    ]
    return [
        {"role": "system", "content": NOVELIST_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]


def build_continuation_messages(
    *, messages: list[ChatMessage], generated: str, current_words: int, target_words: int
) -> list[ChatMessage]:
    """整章生成字数不足时的续写：在原对话后追加已生成正文（assistant）与续写指令（user）。"""
    remaining = max(target_words - current_words, 300)
    instruction = "\n".join(
        [
            f"本章目前约 {current_words} 字，目标约 {target_words} 字，还差约 {remaining} 字。",
            f"请紧接上文继续写本章后续内容，约 {remaining} 字：",
            "1. 从新的段落开始，不要重复、复述或改写已写内容，不要另起新章节或输出章节标题。",
            "2. 保持人物、设定与文风一致，情节自然推进，围绕本章梗概收束，章末留有钩子。",
            f"3. {PROSE_RULES}",
        ]
    )
    return [
        *messages,
        {"role": "assistant", "content": generated},
        {"role": "user", "content": instruction},
    ]


# ===== rewrite.py =====
RewriteOp = Literal["rewrite", "expand", "condense", "deslop", "continue"]

OP_LABELS: dict[str, str] = {
    "rewrite": "改写",
    "expand": "扩写",
    "condense": "缩写",
    "deslop": "去 AI 味",
    "continue": "续写",
}

_OP_RULES: dict[str, list[str]] = {
    "rewrite": [
        "改写【待处理片段】：保留原有情节、人物行为与信息点，换一种更好的表达，使语言更流畅、更有画面感。",
        "篇幅与原片段大致相当。",
    ],
    "expand": [
        "扩写【待处理片段】：保留原有情节与信息点，补充动作、对话、心理与环境细节，让场景更饱满。",
        "篇幅约为原片段的 1.5～2 倍，不要引入与前后文冲突的新情节。",
    ],
    "condense": [
        "缩写【待处理片段】：保留关键情节、人物行为与必要信息，删去冗余描写与重复表达。",
        "篇幅约为原片段的一半，读起来仍然连贯。",
    ],
    "deslop": [
        "去除【待处理片段】中的 AI 腔：删掉套话、空洞的总结与升华、堆砌的排比和比喻，"
        "以及“仿佛”“不禁”“嘴角勾起一抹弧度”“眼中闪过一丝”之类的模板化表达。",
        "改用具体的动作、细节和符合人物身份的口语，保留原有情节与信息点，篇幅与原片段大致相当。",
    ],
    "continue": [
        "从【前文】结尾处继续往下写，约 500～1000 字（作者另有要求时以作者要求为准）。",
        "不要重复或复述前文，情节自然推进，保持人物与设定一致。",
    ],
}


def build_rewrite_messages(
    *,
    op: RewriteOp,
    selected: str,
    before: str,
    after: str,
    style: str = "",
    summary: str = "",
    instruction: str = "",
) -> list[ChatMessage]:
    parts = [f"请对一章小说正文做局部{OP_LABELS[op]}。"]
    if summary.strip():
        parts.append(f"本章梗概：{summary.strip()}")
    if style.strip():
        parts.append(f"写作风格：{style.strip()}")
    parts += ["", "【前文】", before or "（无）"]
    if op != "continue":
        parts += ["", "【待处理片段】", selected, "", "【后文】", after or "（无）"]
    parts += ["", "【要求】"]
    rules = [*_OP_RULES[op]]
    if op != "continue":
        rules.append("结果要与前文、后文自然衔接，因为它会原样替换【待处理片段】。")
    rules.append("只输出处理后的正文本身，不要输出前文、后文、标题、引号、说明或任何解释；不要使用 Markdown 格式。")
    parts += [f"{i}. {r}" for i, r in enumerate(rules, 1)]
    if instruction.strip():
        parts.append(f"作者额外要求：{instruction.strip()}")
    return [
        {"role": "system", "content": NOVELIST_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]


# ===== analysis.py =====
ANALYST_SYSTEM = (
    "你是一位严谨的中文网络小说编辑，擅长拆解章节结构、追踪人物与设定的变化，并给出可执行的修改建议。"
    "你只依据给出的正文作判断，不臆测正文之外的情节。你的输出始终使用简体中文。"
)

SCORE_RUBRIC = """【评分标准】（三项各 1–10 分，可带一位小数）
- pacing 节奏：情节推进速度是否合适、详略是否得当、有无拖沓或跳跃。
- engagement 吸引力：冲突、悬念与情绪张力能否抓住读者，章末是否有让人想继续读的钩子。
- coherence 连贯性：与人物设定、已知状态、前后逻辑是否一致，有无矛盾或突兀之处。
分档：1–3 明显不合格（问题严重，需要重写）；4–5 勉强及格（问题较多）；6–7 合格（有若干可改进之处）；
8–9 优秀（只有细节可打磨）；10 近乎完美（极少给出）。"""

OUTPUT_SCHEMA = """【输出 JSON 结构】
{
  "summary": "本章摘要，150–300 字，交代关键事件、人物行动与结果",
  "plot_points": [{"text": "情节点", "importance": 1-5}],
  "hooks": [{"type": "悬念|冲突|情感|反转|其他", "strength": 1-10, "text": "钩子内容"}],
  "conflict": {"level": 1-10, "types": ["冲突类型，如 人与人、人与自我、人与环境"]},
  "emotion": {"tone": "整体基调", "start": "开头情绪", "middle": "中段情绪", "end": "结尾情绪"},
  "character_changes": [{"name": "角色名", "current_state": "变化后的当前处境（可省略）",
                          "life_status": "alive|dead|missing|other（仅当本章发生生死 / 失踪变化时给出）"}],
  "relationship_changes": [{"a": "角色A", "b": "角色B", "intimacy": -100到100的整数（变化后的值，可省略）,
                             "status": "normal|broken|past|complicated（可省略）",
                             "description": "变化后的关系描述（可省略）",
                             "new_relation": {"role_a": "A 是 B 的什么", "role_b": "B 是 A 的什么",
                                              "category": "family|social|hostile|work|other"}}],
  "career_changes": [{"name": "角色名", "career": "职业名", "stage": "变化后所处的阶段名"}],
  "organization_changes": [{"name": "角色名", "organization": "组织名", "action": "join|leave|update",
                             "position": "职位（可省略）", "status": "active|left|expelled|dead（可省略）"}],
  "foreshadows": [{"action": "plant|resolve", "text": "伏笔内容", "keyword": "关键词",
                   "expected_resolve_chapter": 章节号或null}],
  "scores": {"pacing": 1-10, "engagement": 1-10, "coherence": 1-10},
  "suggestions": [{"category": "节奏|对话|描写|冲突|人物|其他", "priority": "high|medium|low",
                   "text": "具体、可执行的修改建议"}]
}"""

_FORESHADOW_SCHEMA = """  "foreshadows": [{"action": "plant|resolve", "text": "伏笔内容", "keyword": "关键词",
                   "expected_resolve_chapter": 章节号或null}],"""
_FORESHADOW_SCHEMA_REF = """  "foreshadows": [{"action": "plant|resolve", "ref_id": 已有伏笔编号或null,
                   "text": "伏笔内容", "keyword": "关键词",
                   "expected_resolve_chapter": 章节号或null, "partial": true或false}],"""
# 有已有伏笔清单时使用：增加 ref_id / partial；没有伏笔时提示词与原来完全一致
OUTPUT_SCHEMA_WITH_FORESHADOW_REFS = OUTPUT_SCHEMA.replace(_FORESHADOW_SCHEMA, _FORESHADOW_SCHEMA_REF)
assert OUTPUT_SCHEMA_WITH_FORESHADOW_REFS != OUTPUT_SCHEMA

RULES = """【要求】
1. 设定变化只报告本章正文里确实发生的变化；没有变化的角色、关系、职业、组织不要输出。
2. 所有变化都给出「变化后的值」（例如亲密度变化后是 20，就写 20），不要写增量。
3. 角色、组织、职业、阶段名称必须使用参考资料中的原名；参考资料里没有的角色也可以写出名字，但不要编造设定。
4. new_relation 只在两人此前没有关系、本章新结成关系时给出；已有关系的变化只写 intimacy / status / description。
5. career_changes 只针对角色已挂接的职业，stage 必须是该职业阶梯中的阶段名。
6. 建议数量与分数挂钩：三项平均低于 5 分给 3–5 条，5–8 分给 2–4 条，8 分以上给 0–2 条；每条建议要指出具体位置或做法。
7. 没有的内容输出空数组或空对象，不要省略字段。"""

FORESHADOW_RULE = (
    "8. foreshadows：本章埋下或回收【已有伏笔】中的伏笔时，必须在 ref_id 填写该伏笔的编号（只填数字）；"
    "本章新埋下的伏笔 ref_id 填 null；回收只针对已有伏笔；只揭示了一部分时 partial 填 true。"
    "只报告本章正文里确实发生的埋设与回收。"
)


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
) -> list[ChatMessage]:
    """foreshadows_text 为空时（项目没有可引用的伏笔）提示词与引入伏笔管理之前逐字一致。"""
    has_refs = bool(foreshadows_text.strip())
    parts = ["请分析下面这一章小说正文。", "", "【作品信息】", format_project_info(info)]
    if cast_text.strip():
        parts += ["", "【本章涉及角色的当前状态】", cast_text.strip()]
    if relations_text.strip():
        parts += ["", "【这些角色之间的现有关系】", relations_text.strip()]
    if organizations_text.strip():
        parts += ["", "【项目中的组织】", organizations_text.strip()]
    if careers_text.strip():
        parts += ["", "【相关职业的阶段阶梯】", careers_text.strip()]
    if has_refs:
        parts += ["", "【已有伏笔】（#编号 标题｜内容｜状态｜计划回收章）", foreshadows_text.strip()]
    parts += [
        "",
        f"【正文】第{chapter_no}章 {title}".rstrip(),
        content,
        "",
        SCORE_RUBRIC,
        "",
        OUTPUT_SCHEMA_WITH_FORESHADOW_REFS if has_refs else OUTPUT_SCHEMA,
        "",
        RULES + ("\n" + FORESHADOW_RULE if has_refs else ""),
        "",
        JSON_ONLY_RULE,
    ]
    return [
        {"role": "system", "content": ANALYST_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]


# ===== entities.py =====
RELATION_CATEGORY_HINT = "category 取值：family（家庭）、social（社交）、hostile（敌对）、work（职场）、other（其他）"
# 有职业时追加的要求与输出字段（没有职业时提示词与原来一致）
CAREER_ASSIGN_RULE = (
    "careers 为该角色的职业（可为空数组）：name 必须是上面列出的已有职业名称，stage 必须是该职业阶梯中的阶段名称，"
    "role 为 main（主职业，只能是主修职业，最多 1 个）或 sub（副职业，只能是副修职业，最多 3 个），"
    "note 为状态备注（如「瓶颈期」，可为空）。"
)
CAREER_ASSIGN_EXAMPLE = '[{"name": "剑修", "stage": "筑基", "role": "main", "note": ""}]'


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
    careers_text: str = "",
) -> list[ChatMessage]:
    want = f"定位为「{role_type.strip()}」的" if role_type.strip() else ""
    has_careers = bool(careers_text.strip())
    career_rule = f"5. {CAREER_ASSIGN_RULE}\n" if has_careers else ""
    json_no = 6 if has_careers else 5
    career_field = f',\n  "careers": {CAREER_ASSIGN_EXAMPLE}' if has_careers else ""
    user = (
        f"请为以下小说设计一个{want}新角色，并建议他与已有角色之间的关系。\n\n"
        f"{format_project_info(info)}"
        f"{_block('世界观', world_text)}"
        f"{_block('已有角色（名字 / 定位 / 别名）', roster_text)}"
        f"{_block('已有组织', organizations_text)}"
        f"{_block('已有职业（名称 / 类别 / 阶梯）', careers_text)}"
        f"{_block('作者额外要求', instruction)}\n\n"
        "【要求】\n"
        "1. 新角色的名字和别名不能与已有角色的名字或别名重复。\n"
        "2. role_type 取值：主角、重要配角、配角、反派、其他。\n"
        "3. appearance / personality / background 各 50~200 字，profile 为 100 字以内的一句话简介。\n"
        "4. relationships 只能指向上面列出的已有角色，target 必须使用已有角色的名字；没有合适的可以为空数组。\n"
        "   self_role 是新角色在这段关系中的身份，target_role 是对方的身份（如 self_role=徒弟、target_role=师父；"
        "对称关系两者相同，如 朋友/朋友）。\n"
        f"   {RELATION_CATEGORY_HINT}；intimacy 为 -100~100 的整数（敌对为负）。\n"
        f"{career_rule}"
        f"{json_no}. {JSON_ONLY_RULE}\n\n"
        "【输出格式】\n"
        "{\n"
        '  "character": {"name": "角色名", "aliases": ["别名"], "role_type": "配角", "age": "18", "gender": "男",\n'
        '    "appearance": "外貌", "personality": "性格", "background": "背景", "profile": "简介"},\n'
        '  "relationships": [\n'
        '    {"target": "已有角色名", "self_role": "徒弟", "target_role": "师父", "category": "social",\n'
        '     "intimacy": 60, "description": "关系描述"}\n'
        f"  ]{career_field}\n"
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


# ===== careers.py =====
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


# _block 同上（三个模块中的定义完全相同）


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


# ===== wizard.py =====
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


# ===== outline_pro.py =====
# _block 同上（三个模块中的定义完全相同）


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
