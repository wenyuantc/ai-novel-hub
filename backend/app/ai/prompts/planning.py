"""只供代码使用的规划片段；完整块由章节模板的锁定变量保护。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.ai.templating import render_text

PlanningScope = Literal["chapter", "selection"]
_TEMPLATE = (Path(__file__).parent / "templates" / "planning.context.j2").read_text(encoding="utf-8").rstrip("\n")


@dataclass(frozen=True)
class PlanningTextItem:
    title: str
    body: str


def render_planning_context(
    items: list[PlanningTextItem], *, scope: PlanningScope, truncated: bool = False, planned: bool = False
) -> str:
    if not items:
        return ""
    return render_text(_TEMPLATE, {"items": items, "scope": scope, "truncated": truncated, "planned": planned}).strip()
