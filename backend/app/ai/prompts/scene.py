"""不可编辑的场景范围块，作为章节模板的锁定变量注入。"""

from pathlib import Path
from typing import Any

from app.ai.templating import TemplateVar, render_messages

_TEMPLATE = (Path(__file__).parent / "templates" / "scene.scope.user.j2").read_text(encoding="utf-8").rstrip("\n")
_VARIABLES = tuple(TemplateVar(name, name, kind) for name, kind in (
    ("scene_id", "int"), ("title", "text"), ("summary", "text"), ("index", "int"), ("total", "int"),
    ("target_words", "int"), ("preceding_text", "text"), ("selection", "bool"),
))


def render_scene_scope(values: dict[str, Any]) -> str:
    return render_messages(_VARIABLES, "", _TEMPLATE, values, key="scene.scope").messages[-1]["content"]
