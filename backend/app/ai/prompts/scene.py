"""不可编辑的场景范围块和只读拆并建议模板。"""

from pathlib import Path
from typing import Any

from app.ai.providers.base import ChatMessage
from app.ai.templating import TemplateVar, render_messages

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_TEMPLATE = (_TEMPLATE_DIR / "scene.scope.user.j2").read_text(encoding="utf-8").rstrip("\n")
_VARIABLES = tuple(TemplateVar(name, name, kind) for name, kind in (
    ("scene_id", "int"), ("title", "text"), ("summary", "text"), ("index", "int"), ("total", "int"),
    ("target_words", "int"), ("preceding_text", "text"), ("selection", "bool"),
))
_RESTRUCTURE_SYSTEM = (_TEMPLATE_DIR / "scene.restructure.system.j2").read_text(encoding="utf-8").removesuffix("\n")
_RESTRUCTURE_USER = (_TEMPLATE_DIR / "scene.restructure.user.j2").read_text(encoding="utf-8").removesuffix("\n")
_RESTRUCTURE_VARIABLES = tuple(TemplateVar(name, name, kind) for name, kind in (
    ("operation", "text"), ("candidates", "list"), ("left", "text"), ("right", "text"),
))


def render_scene_scope(values: dict[str, Any]) -> str:
    return render_messages(_VARIABLES, "", _TEMPLATE, values, key="scene.scope").messages[-1]["content"]


def build_restructure_messages(
    *, operation: str, candidates: list[tuple[int, int]], left: str, right: str = "",
) -> list[ChatMessage]:
    values = {"operation": operation, "candidates": candidates, "left": left, "right": right}
    return render_messages(
        _RESTRUCTURE_VARIABLES, _RESTRUCTURE_SYSTEM, _RESTRUCTURE_USER, values, key="scene.restructure",
    ).messages
