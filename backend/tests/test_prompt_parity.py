"""逐字一致：系统默认模板的渲染结果 == 改造前的提示词（tests/prompt_legacy.py）。

每个 key 覆盖全部条件分支。之后有意修改默认模板时，同步修改 prompt_legacy 对应函数或删掉对应用例。
"""

import pytest

from app.ai import prompts as new
from app.ai.prompts.common import ProjectInfo
from app.ai.prompts.registry import TEMPLATE_KEYS
from tests import prompt_legacy as old

FULL_INFO = ProjectInfo(title="星海纪元", genre="科幻", premise="人类首次接触外星文明", style="冷峻克制")
BARE_INFO = ProjectInfo(title="无题")
MULTILINE = "第一行\n第二行 {{ 不是模板 }} {% raw %}\n"

CASES: list[tuple[str, str, dict]] = []


def case(key: str, fn: str, **kwargs) -> None:
    CASES.append((key, fn, kwargs))


# ---- chapter.generate
CTX = "【作品信息】\n书名：《星海纪元》\n\n【前情提要】\n第1章：……"
case(
    "chapter.generate", "build_chapter_messages", context=CTX, chapter_no=3, title="初遇", summary="", target_words=3000
)
case(
    "chapter.generate",
    "build_chapter_messages",
    context=CTX,
    chapter_no=3,
    title="",
    summary="  林远收到信号  ",
    target_words=2500,
    style=" 冷峻克制\n短句 ",
    instruction=" 多写对话 ",
    key_events=["收到信号", "  ", " 舰长下令 "],
    goal="引出外星文明",
    emotion=" 紧张 ",
)
case(
    "chapter.generate",
    "build_chapter_messages",
    context=MULTILINE,
    chapter_no=10,
    title=" 标题 ",
    summary="梗概",
    target_words=100,
    key_events=[],
    goal="  ",
    emotion="",
    style="",
    instruction="",
)
# ---- chapter.polish
case("chapter.polish", "build_polish_messages", content="原文\n\n第二段")
case("chapter.polish", "build_polish_messages", content=MULTILINE, style="凌厉", instruction="去掉形容词")
case("chapter.polish", "build_polish_messages", content="", style="  ", instruction=" x ")
# ---- chapter.revise
case(
    "chapter.revise",
    "build_revise_messages",
    context=CTX,
    chapter_no=2,
    title="初遇",
    content="原文",
    suggestions=[],
    target_words=3000,
)
case(
    "chapter.revise",
    "build_revise_messages",
    context=CTX,
    chapter_no=2,
    title="",
    content=MULTILINE,
    suggestions=["加强悬念", "减少旁白"],
    target_words=1200,
    style="冷峻",
    instruction=" 结尾改为开放式 ",
    preserve=["舰长台词", " ", "结尾"],
)
case(
    "chapter.revise",
    "build_revise_messages",
    context=CTX,
    chapter_no=2,
    title="x",
    content="原文",
    suggestions=["一条"],
    target_words=300,
    preserve=[],
)
# ---- chapter.continue
BASE = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
case(
    "chapter.continue",
    "build_continuation_messages",
    messages=BASE,
    generated="正文",
    current_words=100,
    target_words=3000,
)
case(
    "chapter.continue",
    "build_continuation_messages",
    messages=BASE,
    generated="正文",
    current_words=2900,
    target_words=3000,
)
# ---- chapter.rewrite（5 种操作 × 有无可选项）
for op in ("rewrite", "expand", "condense", "deslop", "continue"):
    case("chapter.rewrite", "build_rewrite_messages", op=op, selected="他很生气。", before="", after="")
    case(
        "chapter.rewrite",
        "build_rewrite_messages",
        op=op,
        selected=MULTILINE,
        before="前文\n",
        after="后文",
        style=" 冷峻 ",
        summary=" 梗概 ",
        instruction=" 更紧凑 ",
    )
# ---- analysis.chapter
case(
    "analysis.chapter", "build_analysis_messages", info=BARE_INFO, chapter_no=1, title="", content="正文", cast_text=""
)
case(
    "analysis.chapter",
    "build_analysis_messages",
    info=FULL_INFO,
    chapter_no=5,
    title="决战",
    content=MULTILINE,
    cast_text=" 林远：存活 ",
    relations_text="林远 与 张三：朋友",
    organizations_text="■ 舰队",
    careers_text="剑修：练气 → 筑基",
    foreshadows_text="",
)
case(
    "analysis.chapter",
    "build_analysis_messages",
    info=FULL_INFO,
    chapter_no=5,
    title="决战",
    content="正文",
    cast_text="",
    relations_text="  ",
    foreshadows_text=" #1 神秘信号｜…｜埋下｜第 9 章 ",
)
# ---- wizard
for existing in ("", "  ", "第1卷 …\n第1章 …"):
    case(
        "wizard.outline",
        "build_outline_messages",
        info=FULL_INFO,
        volume_count=2,
        chapters_per_volume=10,
        existing_text=existing,
        start_volume_no=3,
        start_chapter_no=21,
    )
case(
    "wizard.outline",
    "build_outline_messages",
    info=BARE_INFO,
    volume_count=1,
    chapters_per_volume=5,
    world_text=" 世界观\n",
    characters_text="角色",
    instruction=" 要求 ",
)
case("wizard.characters", "build_characters_messages", info=FULL_INFO, character_count=6)
case(
    "wizard.characters",
    "build_characters_messages",
    info=BARE_INFO,
    character_count=3,
    world_text="世界观 ",
    outline_text=" 大纲",
    instruction="要求",
    careers_text="剑修（主修）：练气 → 筑基",
)
case("wizard.characters", "build_characters_messages", info=FULL_INFO, character_count=6, careers_text="  ")
case("wizard.world", "build_world_messages", info=FULL_INFO)
case(
    "wizard.world",
    "build_world_messages",
    info=BARE_INFO,
    outline_text="大纲\n",
    characters_text=" 角色",
    instruction=" 要求 ",
)
case("wizard.careers", "build_careers_wizard_messages", info=FULL_INFO, career_count=4)
case(
    "wizard.careers",
    "build_careers_wizard_messages",
    info=BARE_INFO,
    career_count=4,
    world_text=" 世界 ",
    instruction="x",
)
# ---- entity
case("entity.character", "build_character_generate_messages", info=FULL_INFO)
case(
    "entity.character",
    "build_character_generate_messages",
    info=BARE_INFO,
    world_text="世界",
    roster_text="林远 / 主角",
    organizations_text="舰队",
    role_type=" 反派 ",
    instruction="要求",
    careers_text="剑修",
)
case("entity.organization", "build_organization_generate_messages", info=FULL_INFO)
case(
    "entity.organization",
    "build_organization_generate_messages",
    info=BARE_INFO,
    world_text=" 世界 ",
    roster_text="林远",
    organizations_text="舰队",
    instruction="要求",
)
for kind in (None, "main", "sub"):
    case("entity.career", "build_career_generate_messages", info=FULL_INFO, count=2, kind=kind)
case(
    "entity.career",
    "build_career_generate_messages",
    info=BARE_INFO,
    count=1,
    world_text="世界",
    careers_text=" 剑修 ",
    instruction="要求",
)
case(
    "relations.parse",
    "build_relations_parse_messages",
    info=FULL_INFO,
    character_name="林远",
    relations_text="师父是张三",
)
case(
    "relations.parse",
    "build_relations_parse_messages",
    info=BARE_INFO,
    character_name="林远",
    relations_text="  ",
    roster_text="张三 / 配角",
)
# ---- outline
case(
    "outline.continue",
    "build_outline_continue_messages",
    info=FULL_INFO,
    count=5,
    next_chapter_no=11,
    volume_hint="接在当前卷之后。",
)
case(
    "outline.continue",
    "build_outline_continue_messages",
    info=BARE_INFO,
    count=3,
    next_chapter_no=1,
    volume_hint=" 开新卷 ",
    world_text="世界",
    recent_outlines_text="第10章 …",
    story_so_far_text=" 摘要 ",
    foreshadows_text="#1 信号",
    direction="走向决战",
)
case(
    "outline.expand",
    "build_outline_expand_messages",
    info=FULL_INFO,
    outline_text="第3章 初战",
    neighbors_text="",
    roster_text="",
    kept_text="",
    previous_events_text="",
    count=3,
    total=3,
)
case(
    "outline.expand",
    "build_outline_expand_messages",
    info=BARE_INFO,
    outline_text=" 第3章 初战 ",
    neighbors_text="第2章",
    roster_text="林远",
    kept_text="细纲1",
    previous_events_text="事件A",
    count=2,
    total=5,
    instruction="要求",
)


def _ids() -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for key, _, _ in CASES:
        seen[key] = seen.get(key, 0) + 1
        out.append(f"{key}-{seen[key]}")
    return out


@pytest.mark.parametrize(("key", "fn", "kwargs"), CASES, ids=_ids())
def test_default_template_matches_legacy(key, fn, kwargs):
    kwargs = dict(kwargs)
    info = kwargs.pop("info", None)
    args = (info,) if info is not None else ()
    expected = getattr(old, fn)(*args, **kwargs)
    actual = getattr(new, fn)(*args, **kwargs)
    assert actual == expected


def test_every_legacy_key_is_covered():
    covered = {key for key, _, _ in CASES}
    # style.* / inspiration.* / wizard.continue 是新增模板，没有改造前的对照
    assert covered == {
        k for k in TEMPLATE_KEYS if not k.startswith(("style.", "inspiration.")) and k != "wizard.continue"
    }
