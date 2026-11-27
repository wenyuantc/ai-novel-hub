"""校验模型返回的业务结构；引用归属、版本和目标集合由任务事务核实。"""

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.ai.json_extract import extract_json

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=60000)]
RequiredText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60000)]
EntityID = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Look(_Output):
    name: Name
    styling: Text = ""


class Character(_Output):
    name: Name
    role: Label = ""
    description: Text = ""
    appearance: Text = ""
    looks: list[Look] = Field(min_length=1)


class Scene(_Output):
    location: Name
    time_of_day: Name
    description: Text = ""
    lighting: Text = ""


class Prop(_Output):
    name: Name
    category: Label = ""
    description: Text = ""


class Extraction(_Output):
    characters: list[Character]
    scenes: list[Scene]
    props: list[Prop] = Field(max_length=3)


class Shot(_Output):
    title: Title
    description: RequiredText
    shot_size: Title
    duration_seconds: int = Field(ge=2, le=30)
    atmosphere: Text = ""
    scene_id: EntityID | None = None
    look_ids: list[EntityID] = Field(default_factory=list, max_length=200)
    prop_ids: list[EntityID] = Field(default_factory=list, max_length=200)
    video_prompt: Text = ""

    @field_validator("look_ids", "prop_ids")
    @classmethod
    def unique_refs(cls, refs: list[int]) -> list[int]:
        if len(set(refs)) != len(refs):
            raise ValueError("分镜的素材引用不能重复")
        return refs


class Storyboard(_Output):
    shots: list[Shot] = Field(min_length=1, max_length=200)


class Prompt(_Output):
    kind: Literal["look", "scene", "prop", "shot"]
    id: EntityID
    prompt: RequiredText


class PromptBatch(_Output):
    prompts: list[Prompt] = Field(min_length=1)

    @field_validator("prompts")
    @classmethod
    def unique_targets(cls, prompts: list[Prompt]) -> list[Prompt]:
        if len({(p.kind, p.id) for p in prompts}) != len(prompts):
            raise ValueError("提示词目标不能重复")
        return prompts


_SCENE_HEADER = re.compile(r"##[ \t]+S(\d+)[ \t]*\|[ \t]*(?:内景|外景)[ \t]*·[ \t]*[^|\n]+?\s*\|[ \t]*[^|\n]+")
_STRUCTURED = {"drama.extract": Extraction, "drama.storyboard": Storyboard, "drama.prompts": PromptBatch}


def _script(text: str) -> str:
    script = text.strip()
    if len(script) > 200_000:
        raise ValueError("剧本超过 20 万字")
    scenes = re.split(r"(?m)(?=^##[ \t])", script)
    if scenes[0] or len(scenes) == 1:
        raise ValueError("剧本须从格式化场景头开始")
    for index, scene in enumerate(scenes[1:], start=1):
        header, _, action = scene.partition("\n")
        match = _SCENE_HEADER.fullmatch(header.strip())
        if not match or int(match[1]) != index or not action.strip():
            raise ValueError(f"第 {index} 场需要连续编号、内外景、地点、时间段及正文")
    return script


def parse_output(kind: str, text: str) -> dict[str, Any]:
    if kind == "drama.rewrite":
        return {"script": _script(text)}
    if kind not in _STRUCTURED:
        raise ValueError("不支持的短剧任务类型")
    return _STRUCTURED[kind].model_validate(extract_json(text)).model_dump()
