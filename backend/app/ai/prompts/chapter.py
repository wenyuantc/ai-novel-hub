"""章节生成与润色提示词。"""

from app.ai.prompts.common import NOVELIST_SYSTEM
from app.ai.providers.base import ChatMessage

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
) -> list[ChatMessage]:
    parts = [
        "以下是创作参考资料：",
        context,
        "",
        f"【本章任务】第{chapter_no}章 {title}".rstrip(),
    ]
    if summary.strip():
        parts.append(f"本章梗概：{summary.strip()}")
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
