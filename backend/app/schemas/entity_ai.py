"""设定类 AI 生成、人物关系构思与旧关系备注解析的请求。"""

from pydantic import BaseModel, Field, field_validator


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


class RelationsGenerateRequest(BaseModel):
    character_ids: list[int] = Field(min_length=2, max_length=20)
    count: int = Field(default=10, ge=1, le=50, strict=True)
    instruction: str = Field(default="", max_length=2000)
    ai_config_id: int | None = None

    @field_validator("character_ids", mode="before")
    @classmethod
    def validate_character_ids(cls, value: object) -> object:
        if isinstance(value, list):
            if not 2 <= len(value) <= 20:
                raise ValueError("请选择 2–20 名角色")
            if any(type(cid) is not int or cid <= 0 for cid in value):
                raise ValueError("角色 ID 必须是正整数")
            if len(set(value)) != len(value):
                raise ValueError("不能重复选择同一角色")
        return value
