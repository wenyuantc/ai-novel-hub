from dataclasses import dataclass

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
    # 立项结构化字段（0014）：非空时依次追加在原有几行之后；全空时输出与改造前逐字一致
    synopsis: str = ""
    theme: str = ""
    tags: tuple[str, ...] = ()
    selling_points: str = ""
    audience: str = ""


def format_project_info(info: ProjectInfo) -> str:
    lines = [f"书名：《{info.title}》"]
    if info.genre:
        lines.append(f"题材：{info.genre}")
    if info.premise:
        lines.append(f"一句话设定：{info.premise}")
    if info.style:
        lines.append(f"写作风格：{info.style}")
    tags = [t for t in (t.strip() for t in info.tags) if t]
    if tags:
        lines.append(f"类型标签：{'、'.join(tags)}")
    for label, value in (
        ("主题", info.theme),
        ("简介", info.synopsis),
        ("核心卖点", info.selling_points),
        ("目标读者", info.audience),
    ):
        if value.strip():
            lines.append(f"{label}：{value.strip()}")
    return "\n".join(lines)
