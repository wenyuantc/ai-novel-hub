"""提示词模板注册表：28 个模板 key 的元信息、变量声明与系统默认模板（templates/<key>.{system,user}.j2）。

- 数据块（参考资料、角色卡、作品信息等）由 build_*_messages 在 Python 里拼好后作为变量传入，模板只负责排布与措辞。
- 锁定变量（json_rule / output_format / output_schema）的内容由代码提供，模板里必须原样出现一次（见 templating）。
- 系统默认的渲染结果与改造前的提示词逐字一致，由 tests/test_prompt_parity.py 守护。
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ai.prompts import formats as F
from app.ai.prompts.common import JSON_ONLY_RULE
from app.ai.providers.base import ChatMessage
from app.ai.templating import PromptOverride, TemplateRenderError, TemplateVar, VarKind, render_messages

TEMPLATE_DIR = Path(__file__).parent / "templates"

CATEGORIES: dict[str, str] = {
    "chapter": "章节",
    "analysis": "分析",
    "wizard": "向导",
    "entity": "设定生成",
    "outline": "大纲",
    "style": "写作风格",
    "inspiration": "灵感模式",
    "book_import": "拆书",
}


@dataclass(frozen=True)
class PromptTemplate:
    key: str
    name: str
    category: str
    description: str
    system: str
    user: str
    variables: tuple[TemplateVar, ...]

    @property
    def locked(self) -> tuple[TemplateVar, ...]:
        return tuple(v for v in self.variables if v.locked)


def _load(key: str, part: str) -> str:
    text = (TEMPLATE_DIR / f"{key}.{part}.j2").read_text(encoding="utf-8")
    # 文件末尾的一个换行只是编辑器习惯，不属于模板内容
    return text[:-1] if text.endswith("\n") else text


def T(name: str, label: str, kind: VarKind = "text", *, required: bool = False, sample: Any = "") -> TemplateVar:  # noqa: N802
    return TemplateVar(name=name, label=label, kind=kind, required=required, sample=sample)


def L(name: str, label: str, sample: str) -> TemplateVar:  # noqa: N802
    return TemplateVar(name=name, label=label, kind="text", required=True, locked=True, sample=sample)


SAMPLE_PROJECT_INFO = "书名：《星海纪元》\n题材：科幻\n一句话设定：人类首次接触外星文明"
SAMPLE_CONTEXT = "【作品信息】\n" + SAMPLE_PROJECT_INFO + "\n\n【前情提要】\n第1章 启航：林远登上远征舰。"
SAMPLE_STYLE = "叙事视角：第三人称有限视角\n句式节奏：短句为主，节奏紧凑"

JSON_RULE_VAR = L("json_rule", "只输出 JSON 的规则（锁定）", JSON_ONLY_RULE)
PROJECT_INFO_VAR = T(
    "project_info", "作品信息（书名 / 题材 / 设定 / 风格概述）", required=True, sample=SAMPLE_PROJECT_INFO
)
INSTRUCTION_VAR = T("instruction", "作者额外要求", sample="")
# 检索块（knowledge-base）：为空时模板不输出对应段落
KNOWLEDGE_VAR = T(
    "knowledge", "知识库参考资料（自动检索，可能为空）", sample="- 《设定集》门派 > 天剑宗：天剑宗位于北境雪山。"
)
MEMORIES_VAR = T("memories", "相关旧情节（剧情记忆召回，可能为空）", sample="- 第3章（情节）：林远拜入天剑宗。")


def _out(sample: str) -> TemplateVar:
    """锁定的输出结构由各 build 函数按分支提供；这里的 sample 只用于预览。"""
    return L("output_format", "JSON 输出格式（锁定）", sample)


def _inspiration(key: str, name: str, desc: str) -> tuple[str, str, str, str, tuple[TemplateVar, ...]]:
    """灵感模式各步骤共用同一组变量；步骤约束（数量、字数）写在各自的模板里。"""
    return (
        f"inspiration.{key}",
        f"灵感：{name}",
        "inspiration",
        desc,
        (
            T("idea", "创作想法", required=True, sample="一个能听见城市心跳的少女，在末日前夜寻找失踪的哥哥。"),
            T("notes", "补充想法（每行一条）", sample="- 希望结局温暖一些"),
            T("decided", "已经定下的内容", sample="题材：都市奇幻\n主题：在失去中学会告别"),
            T("history", "本步之前各轮的选项与反馈", sample=""),
            T("feedback", "这次的反馈", sample="再短一点，更有画面感"),
            KNOWLEDGE_VAR,
            JSON_RULE_VAR,
            _out(F.INSPIRATION_FORMAT),
        ),
    )


_DEFS: list[tuple[str, str, str, str, tuple[TemplateVar, ...]]] = [
    (
        "chapter.generate",
        "整章生成",
        "chapter",
        "按参考资料与本章任务生成一整章正文。",
        (
            T("context", "创作参考资料（作品信息、世界观、角色、大纲、前情）", required=True, sample=SAMPLE_CONTEXT),
            T("chapter_no", "章节号", "int", sample=2),
            T("title", "章节标题", sample="初遇"),
            T("heading", "章节标题行（第N章 标题）", sample="第2章 初遇"),
            T("summary", "本章梗概", sample="林远在舰桥第一次收到异星信号。"),
            T("key_events", "关键事件（来自细纲）", "list", sample=["收到信号", "舰长下令追踪"]),
            T("goal", "叙事目标（来自细纲）", sample="引出外星文明"),
            T("emotion", "情感基调（来自细纲）", sample="紧张"),
            T("target_words", "目标字数", "int", sample=3000),
            T("style", "写作风格段（由风格库渲染）", sample=SAMPLE_STYLE),
            INSTRUCTION_VAR,
        ),
    ),
    (
        "chapter.continue",
        "字数不足自动续写",
        "chapter",
        "整章生成字数不足时追加的续写指令（接在原对话与已生成正文之后，只有 user 段）。",
        (
            T("current_words", "当前字数", "int", sample=1800),
            T("target_words", "目标字数", "int", sample=3000),
            T("remaining", "还差的字数", "int", sample=1200),
        ),
    ),
    (
        "chapter.polish",
        "整章润色",
        "chapter",
        "润色整章正文，保留情节与信息点。",
        (
            T("content", "章节原文", required=True, sample="林远推开舱门，冷风扑面而来。"),
            T("style", "写作风格段（由风格库渲染）", sample=SAMPLE_STYLE),
            INSTRUCTION_VAR,
        ),
    ),
    (
        "chapter.revise",
        "按建议重写",
        "chapter",
        "按章节分析的修改建议整章重写。",
        (
            T("context", "创作参考资料", required=True, sample=SAMPLE_CONTEXT),
            T("chapter_no", "章节号", "int", sample=2),
            T("title", "章节标题", sample="初遇"),
            T("heading", "章节标题行（第N章 标题）", sample="第2章 初遇"),
            T("content", "章节原文", required=True, sample="林远推开舱门，冷风扑面而来。"),
            T("suggestions", "勾选的修改建议", "list", sample=["加强开头的悬念", "减少旁白"]),
            T("preserve", "必须保留的要素", "list", sample=["舰长的台词"]),
            T("target_words", "目标字数", "int", sample=3000),
            T("style", "写作风格段（由风格库渲染）", sample=SAMPLE_STYLE),
            INSTRUCTION_VAR,
        ),
    ),
    (
        "chapter.rewrite",
        "局部改写",
        "chapter",
        "对选中片段改写 / 扩写 / 缩写 / 去 AI 味，或在光标处续写；op 区分操作。",
        (
            T("op", "操作：rewrite / expand / condense / deslop / continue", required=True, sample="rewrite"),
            T("op_label", "操作名称（改写 / 扩写 …）", sample="改写"),
            T("selected", "待处理片段", sample="他很生气。"),
            T("before", "前文（为空时为「（无）」）", sample="林远推开舱门。"),
            T("after", "后文（为空时为「（无）」）", sample="（无）"),
            T("summary", "本章梗概", sample=""),
            T("style", "写作风格段（由风格库渲染）", sample=SAMPLE_STYLE),
            INSTRUCTION_VAR,
        ),
    ),
    (
        "analysis.chapter",
        "章节分析",
        "analysis",
        "分析章节：摘要、情节点、钩子、设定变化、伏笔、评分与建议。",
        (
            PROJECT_INFO_VAR,
            T("chapter_no", "章节号", "int", sample=2),
            T("title", "章节标题", sample="初遇"),
            T("heading", "章节标题行（第N章 标题）", sample="第2章 初遇"),
            T("content", "章节正文", required=True, sample="林远推开舱门，冷风扑面而来。"),
            T("cast", "本章涉及角色的当前状态", sample="林远（主角）：存活；当前状态：刚登舰"),
            T("relations", "这些角色之间的现有关系", sample=""),
            T("organizations", "项目中的组织", sample=""),
            T("careers", "相关职业的阶段阶梯", sample=""),
            T("foreshadows", "已有伏笔清单（有内容时输出结构增加 ref_id / partial）", sample=""),
            L("output_schema", "JSON 输出结构（锁定）", F.ANALYSIS_SCHEMA),
            JSON_RULE_VAR,
        ),
    ),
    (
        "wizard.world",
        "向导：世界观",
        "wizard",
        "生成 5~10 个世界观设定条目。",
        (
            PROJECT_INFO_VAR,
            T("outline", "已有大纲", sample=""),
            T("characters", "已有角色", sample=""),
            KNOWLEDGE_VAR,
            INSTRUCTION_VAR,
            JSON_RULE_VAR,
            _out(F.WORLD_FORMAT),
        ),
    ),
    (
        "wizard.characters",
        "向导：角色",
        "wizard",
        "生成主要角色卡与角色之间的关系；项目有职业时同时分配职业。",
        (
            PROJECT_INFO_VAR,
            T("character_count", "角色数量", "int", sample=6),
            T("world", "已有世界观", sample=""),
            T("outline", "已有大纲", sample=""),
            T("careers", "已有职业体系（有内容时要求分配职业）", sample=""),
            KNOWLEDGE_VAR,
            INSTRUCTION_VAR,
            JSON_RULE_VAR,
            _out(F.characters_wizard_format(False)),
        ),
    ),
    (
        "wizard.outline",
        "向导：大纲",
        "wizard",
        "生成分卷分章大纲；existing 非空时为追加模式。",
        (
            PROJECT_INFO_VAR,
            T("volume_count", "卷数", "int", sample=2),
            T("chapters_per_volume", "每卷章数", "int", sample=10),
            T("world", "已有世界观", sample=""),
            T("characters", "已有角色", sample=""),
            T("existing", "已有大纲末尾（追加模式）", sample=""),
            T("start_volume_no", "起始卷号（追加模式）", "int", sample=1),
            T("start_chapter_no", "起始章节号（追加模式）", "int", sample=1),
            KNOWLEDGE_VAR,
            INSTRUCTION_VAR,
            JSON_RULE_VAR,
            _out(F.OUTLINE_FORMAT),
        ),
    ),
    (
        "wizard.careers",
        "向导：职业体系",
        "wizard",
        "根据世界观设计整套职业体系。",
        (
            PROJECT_INFO_VAR,
            T("career_count", "职业数量", "int", sample=4),
            T("world", "世界观", sample=""),
            KNOWLEDGE_VAR,
            INSTRUCTION_VAR,
            JSON_RULE_VAR,
            _out(F.CAREER_FORMAT),
        ),
    ),
    (
        "wizard.continue",
        "向导：从中断处继续",
        "wizard",
        "向导生成中断后续写的指令（接在原对话与已输出内容之后，只有 user 段）。",
        (T("generated_chars", "已输出的字符数", "int", sample=3200),),
    ),
    (
        "entity.character",
        "AI 生成角色",
        "entity",
        "设计一个新角色，并建议他与已有角色的关系。",
        (
            PROJECT_INFO_VAR,
            T("role_type", "期望的角色定位", sample="配角"),
            T("world", "世界观", sample=""),
            T("roster", "已有角色名单", sample="林远 / 主角"),
            T("organizations", "已有组织", sample=""),
            T("careers", "已有职业（有内容时要求分配职业）", sample=""),
            KNOWLEDGE_VAR,
            INSTRUCTION_VAR,
            JSON_RULE_VAR,
            _out(F.character_generate_format(False)),
        ),
    ),
    (
        "entity.organization",
        "AI 生成组织",
        "entity",
        "设计一个新组织，并建议哪些已有角色属于它。",
        (
            PROJECT_INFO_VAR,
            T("world", "世界观", sample=""),
            T("roster", "已有角色名单", sample="林远 / 主角"),
            T("organizations", "已有组织", sample=""),
            KNOWLEDGE_VAR,
            INSTRUCTION_VAR,
            JSON_RULE_VAR,
            _out(F.ORGANIZATION_FORMAT),
        ),
    ),
    (
        "entity.career",
        "AI 生成职业",
        "entity",
        "新设计若干个职业及其等级阶梯。",
        (
            PROJECT_INFO_VAR,
            T("count", "职业数量", "int", sample=2),
            T("kind", "职业类别：main / sub / 空（不限）", sample=""),
            T("world", "世界观", sample=""),
            T("careers", "已有职业", sample=""),
            KNOWLEDGE_VAR,
            INSTRUCTION_VAR,
            JSON_RULE_VAR,
            _out(F.CAREER_FORMAT),
        ),
    ),
    (
        "relations.parse",
        "解析旧关系备注",
        "entity",
        "把角色的自然语言关系备注解析为结构化关系。",
        (
            PROJECT_INFO_VAR,
            T("character_name", "角色名", required=True, sample="林远"),
            T("relations_text", "关系备注", required=True, sample="师父是张三。"),
            T("roster", "已有角色名单", sample="张三 / 配角"),
            JSON_RULE_VAR,
            _out(F.RELATIONS_PARSE_FORMAT),
        ),
    ),
    (
        "outline.continue",
        "大纲续写",
        "outline",
        "在已有大纲之后续写若干章章纲。",
        (
            PROJECT_INFO_VAR,
            T("count", "续写章数", "int", sample=5),
            T("next_chapter_no", "起始章节号", "int", sample=11),
            T("volume_hint", "分卷说明", sample="接在当前卷「第一卷」之后，不开新卷。"),
            T("world", "世界观", sample=""),
            T("story_so_far", "前文进展（每章摘要）", sample=""),
            T("recent_outlines", "最近的章纲", sample=""),
            T("foreshadows", "尚未回收的伏笔", sample=""),
            KNOWLEDGE_VAR,
            MEMORIES_VAR,
            T("direction", "作者给的发展方向", sample=""),
            JSON_RULE_VAR,
            _out(F.OUTLINE_CONTINUE_FORMAT),
        ),
    ),
    (
        "outline.expand",
        "章纲展开为细纲",
        "outline",
        "把一条章纲展开为多章细纲。",
        (
            PROJECT_INFO_VAR,
            T("outline", "要展开的章纲", required=True, sample="第3章 初战：林远第一次驾驶机甲。"),
            T("neighbors", "前后相邻的章纲", sample=""),
            T("roster", "角色名单", sample=""),
            T("kept", "已写正文、保留不变的细纲", sample=""),
            T("previous_events", "本次已生成的细纲关键事件", sample=""),
            T("count", "本次输出章数", "int", sample=3),
            T("total", "共展开章数", "int", sample=3),
            MEMORIES_VAR,
            KNOWLEDGE_VAR,
            INSTRUCTION_VAR,
            JSON_RULE_VAR,
            _out(F.OUTLINE_EXPAND_FORMAT),
        ),
    ),
    (
        "style.block",
        "风格段",
        "style",
        "把写作风格的各字段渲染成风格段，作为 {{ style }} 注入章节类提示词（只用 user 段，结果去掉首尾空白）。",
        (
            T("name", "风格名称", sample="冷峻克制"),
            T("description", "一句话描述", sample="冷静克制的硬科幻叙事"),
            T("pov", "叙事视角", sample="第三人称有限视角"),
            T("rhythm", "句式节奏", sample="短句为主"),
            T("dialogue", "对白比例", sample="适中"),
            T("description_density", "描写密度", sample="克制"),
            T("tone", "基调", sample="冷峻"),
            T("diction", "用词偏好", sample="技术名词准确"),
            T("taboos", "禁用表达", sample="不用网络流行语"),
            T("sample", "示例段落", sample=""),
            T("extra", "自由补充", sample=""),
        ),
    ),
    (
        "style.extract",
        "从样文提取风格",
        "style",
        "从一段样文归纳写作风格，输出风格草稿（JSON）。",
        (
            T("sample_text", "样文", required=True, sample="夜色像一层薄冰，压在港口的灯上。"),
            JSON_RULE_VAR,
            _out(F.STYLE_EXTRACT_FORMAT),
        ),
    ),
    (
        "style.trial",
        "风格试写",
        "style",
        "用一个写作风格试写一段正文（不写入任何章节）。",
        (
            T("style", "写作风格段", required=True, sample=SAMPLE_STYLE),
            T("scene", "场景描述", required=True, sample="雨夜，主角在旧码头等一个迟到的人。"),
            T("words", "字数", "int", sample=500),
        ),
    ),
    _inspiration("title", "书名", "根据创作想法给出 4~6 个书名候选。"),
    _inspiration("synopsis", "简介", "根据创作想法与已定内容给出 4~6 个作品简介候选。"),
    _inspiration("theme", "主题", "给出 4~6 个作品主题候选。"),
    _inspiration("tags", "类型标签", "给出 6~10 个类型标签候选（可多选）。"),
    _inspiration("selling_points", "核心卖点", "给出 4~6 个核心卖点候选。"),
    _inspiration("audience", "目标读者", "给出 4~6 个目标读者候选。"),
    (
        "book_import.digest",
        "拆书：通读",
        "book_import",
        "拆书导入时按批通读章节：每章摘要，以及本批出现的角色、组织、世界观、职业体系与人物关系。",
        (
            PROJECT_INFO_VAR,
            T("known_characters", "已知角色名单（之前批次累计的姓名与别名）", sample="林远（小林、林师兄）"),
            T(
                "chapters_text",
                "本批章节正文（每段以【第N章 标题】开头）",
                required=True,
                sample="【第1章 雨夜】\n林远推开舱门，冷风扑面而来。",
            ),
            JSON_RULE_VAR,
            _out(F.BOOK_DIGEST_FORMAT),
        ),
    ),
    (
        "book_import.synthesize",
        "拆书：汇总",
        "book_import",
        "拆书导入时汇总各批通读结果：作品信息，以及精选后的角色、组织、世界观、职业体系与人物关系。",
        (
            PROJECT_INFO_VAR,
            T("summaries", "章节摘要（按章号，可能抽样）", sample="第1章 雨夜：林远登上远征舰。"),
            T("characters", "角色草稿", sample="林远｜小林｜主角｜远征舰见习舰员"),
            T("organizations", "组织草稿", sample=""),
            T("world", "世界观草稿", sample=""),
            T("careers", "职业 / 境界体系草稿", sample=""),
            T("relations", "人物关系草稿", sample=""),
            JSON_RULE_VAR,
            _out(F.BOOK_SYNTHESIZE_FORMAT),
        ),
    ),
]

TEMPLATES: dict[str, PromptTemplate] = {
    key: PromptTemplate(
        key=key,
        name=name,
        category=category,
        description=desc,
        system=_load(key, "system"),
        user=_load(key, "user"),
        variables=variables,
    )
    for key, name, category, desc, variables in _DEFS
}
TEMPLATE_KEYS: tuple[str, ...] = tuple(TEMPLATES)


def get_template(key: str) -> PromptTemplate:
    return TEMPLATES[key]


def fingerprint(system: str, user: str) -> str:
    return hashlib.sha256(f"{system}\x00{user}".encode()).hexdigest()[:16]


def default_fingerprint(key: str) -> str:
    tpl = TEMPLATES[key]
    return fingerprint(tpl.system, tpl.user)


def sample_values(key: str) -> dict[str, Any]:
    return {v.name: v.sample for v in TEMPLATES[key].variables}


def render(key: str, override: PromptOverride | None, values: dict[str, Any]) -> list[ChatMessage]:
    """按覆盖（None = 系统默认）渲染；values 必须覆盖该 key 声明的全部变量（缺失属编程错误）。"""
    tpl = TEMPLATES[key]
    missing = [v.name for v in tpl.variables if v.name not in values]
    assert not missing, f"{key} 缺少变量：{missing}"
    system, user = (override.system, override.user) if override is not None else (tpl.system, tpl.user)
    try:
        return render_messages(tpl.variables, system, user, values, key=key).messages
    except TemplateRenderError as exc:
        raise TemplateRenderError(f"提示词模板「{tpl.name}」{exc.detail}，请检查或恢复默认") from exc


def prompt_meta(key: str, override: PromptOverride | None) -> dict[str, Any]:
    """生成记录 params.prompt。"""
    return {
        "key": key,
        "source": "override" if override is not None else "default",
        "override_version_id": override.version_id if override is not None else None,
    }
