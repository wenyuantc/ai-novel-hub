"""待确认章级提案的严格身份和作者位置选择。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Token = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]


class ReviewSectionPosition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scene_id: int = Field(gt=0, le=2_147_483_647)
    section_key: Token
    anchor: int = Field(ge=0, le=2_147_483_647)


class ReviewAccept(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task_key: Token
    run_key: Token
    sections: list[ReviewSectionPosition] = Field(max_length=20_000)
    confirm_format_loss: bool


class ReviewIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task_key: Token
    run_key: Token
    token: Token
