"""章节分析提示词：摘要、情节点、钩子、冲突与情感、设定变化、伏笔候选、评分与改进建议。"""

from app.ai.prompts.common import JSON_ONLY_RULE, ProjectInfo, format_project_info
from app.ai.providers.base import ChatMessage

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
