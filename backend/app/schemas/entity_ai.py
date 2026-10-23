"""AI 单体生成 / 旧关系解析（后台任务 character.generate / organization.generate / relations.parse）的请求。"""

from pydantic import BaseModel, Field


class CharacterGenerateRequest(BaseModel):
    ai_config_id: int | None = None
    instruction: str = Field(default="", max_length=2000)
    # 期望的定位（可选），如「反派」
    role_type: str = Field(default="", max_length=50)


class OrganizationGenerateRequest(BaseModel):
    ai_config_id: int | None = None
    instruction: str = Field(default="", max_length=2000)


class RelationsParseRequest(BaseModel):
    ai_config_id: int | None = None
