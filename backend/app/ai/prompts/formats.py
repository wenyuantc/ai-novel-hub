"""锁定的 JSON 输出结构（解析器依赖这些结构，模板里只能整体引用、不能修改）。"""

ANALYSIS_SCHEMA = """【输出 JSON 结构】
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
# 有已有伏笔清单时使用：增加 ref_id / partial
ANALYSIS_SCHEMA_WITH_FORESHADOW_REFS = ANALYSIS_SCHEMA.replace(_FORESHADOW_SCHEMA, _FORESHADOW_SCHEMA_REF)
assert ANALYSIS_SCHEMA_WITH_FORESHADOW_REFS != ANALYSIS_SCHEMA

CAREER_ASSIGN_EXAMPLE = '[{"name": "剑修", "stage": "筑基", "role": "main", "note": ""}]'

OUTLINE_FORMAT = (
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


def characters_wizard_format(has_careers: bool) -> str:
    career_field = f',\n     "careers": {CAREER_ASSIGN_EXAMPLE}' if has_careers else ""
    return (
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


WORLD_FORMAT = '[\n  {"title": "条目名称", "category": "place", "content": "设定内容"}\n]'

CAREER_FORMAT = (
    "{\n"
    '  "careers": [\n'
    '    {"name": "剑修", "kind": "main", "description": "职业描述", "requirements": "获得条件",\n'
    '     "abilities": "特殊能力", "rules": "体系规则（升阶方式、瓶颈、限制等）",\n'
    '     "stages": [{"name": "练气", "description": "该阶段的特征"}, {"name": "筑基", "description": "..."}]}\n'
    "  ]\n"
    "}"
)


def character_generate_format(has_careers: bool) -> str:
    career_field = f',\n  "careers": {CAREER_ASSIGN_EXAMPLE}' if has_careers else ""
    return (
        "{\n"
        '  "character": {"name": "角色名", "aliases": ["别名"], "role_type": "配角", "age": "18", "gender": "男",\n'
        '    "appearance": "外貌", "personality": "性格", "background": "背景", "profile": "简介"},\n'
        '  "relationships": [\n'
        '    {"target": "已有角色名", "self_role": "徒弟", "target_role": "师父", "category": "social",\n'
        '     "intimacy": 60, "description": "关系描述"}\n'
        f"  ]{career_field}\n"
        "}"
    )


ORGANIZATION_FORMAT = (
    "{\n"
    '  "organization": {"name": "组织名", "org_type": "宗门", "purpose": "宗旨", "location": "所在地",\n'
    '    "power_level": 70, "parent": "", "description": "描述"},\n'
    '  "members": [{"name": "已有角色名", "position": "长老"}]\n'
    "}"
)

RELATIONS_PARSE_FORMAT = (
    "{\n"
    '  "relationships": [\n'
    '    {"target": "张三", "self_role": "徒弟", "target_role": "师父", "category": "social",\n'
    '     "intimacy": 60, "description": "关系描述"}\n'
    "  ]\n"
    "}"
)

OUTLINE_CONTINUE_FORMAT = (
    "{\n"
    '  "volume_title": "（仅开新卷且未指定卷名时填写）",\n'
    '  "outlines": [\n'
    '    {"title": "章节标题", "summary": "本章梗概"}\n'
    "  ]\n"
    "}"
)

OUTLINE_EXPAND_FORMAT = (
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

STYLE_EXTRACT_FORMAT = (
    "{\n"
    '  "name": "风格名称",\n'
    '  "description": "一句话概括",\n'
    '  "pov": "叙事视角",\n'
    '  "rhythm": "句式节奏",\n'
    '  "dialogue": "对白比例",\n'
    '  "description_density": "描写密度",\n'
    '  "tone": "基调",\n'
    '  "diction": "用词偏好",\n'
    '  "taboos": "禁用表达",\n'
    '  "extra": "其他值得模仿的特征"\n'
    "}"
)

INSPIRATION_FORMAT = '{\n  "options": ["选项1", "选项2", "选项3", "选项4"]\n}'

_BOOK_ENTITIES = (
    '  "characters": [{"name": "姓名", "aliases": ["别名"], "role_type": "主角", "profile": "身份与在故事中的作用",\n'
    '                  "personality": "性格", "appearance": "外貌"}],\n'
    '  "organizations": [{"name": "组织名", "org_type": "宗门", "description": "描述",\n'
    '                     "members": [{"name": "角色姓名", "position": "职位"}]}],\n'
    '  "world": [{"title": "条目名称", "category": "place", "content": "设定内容"}],\n'
    '  "careers": [{"name": "剑修", "kind": "main", "description": "体系描述",\n'
    '               "stages": [{"name": "练气", "description": "该阶段的特征"}]}],\n'
    '  "relationships": [{"a": "张三", "b": "李四", "role_a": "师父", "role_b": "徒弟", "category": "social",\n'
    '                     "description": "关系描述"}]\n'
)

BOOK_DIGEST_FORMAT = '{\n  "chapters": [{"chapter_no": 12, "summary": "本章 1~2 句摘要"}],\n' + _BOOK_ENTITIES + "}"

BOOK_SYNTHESIZE_FORMAT = (
    "{\n"
    '  "project": {"synopsis": "作品简介", "theme": "主题", "tags": ["类型标签"], "selling_points": "核心卖点",\n'
    '              "audience": "目标读者", "genre": "主题材"},\n' + _BOOK_ENTITIES + "}"
)
