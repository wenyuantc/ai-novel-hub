"""封面生成弹窗的初始提示词；用户可以直接编辑，不进入文本提示词模板注册表。"""

from app.models import Project, WritingStyle


def build_cover_prompt(project: Project, style: WritingStyle | None) -> str:
    parts = ["请绘制一张竖版 2:3 的小说书籍封面，突出作品的主题、氛围和视觉意象。"]
    fields = [
        ("书名", project.title, 200),
        ("题材", project.genre, 100),
        ("类型标签", "、".join((project.tags or [])[:8]), 80),
        ("设定", project.premise.strip() or (project.synopsis or "")[:300], 800),
    ]
    if style is not None:
        fields.append(("写作风格", "；".join(v for v in (style.name, style.description, style.tone) if v), 500))
    for label, text, limit in fields:
        if text:
            parts.append(f"{label}：{text[:limit]}")
    parts.append("画面中不要出现任何文字、书名、字母、数字或水印。")
    # 各项独立限长，使长设定不会挤掉写作风格与固定画面要求，且总长不超过生成请求的 2000 字上限。
    return "\n".join(parts)
