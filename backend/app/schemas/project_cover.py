from pydantic import BaseModel, Field, field_validator


class CoverGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    config_id: int | None = Field(default=None, gt=0)

    @field_validator("prompt")
    @classmethod
    def clean_prompt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("封面提示词不能为空")
        return value


class CoverPromptOut(BaseModel):
    prompt: str


class CoverGenerateOut(BaseModel):
    image_base64: str
    mime_type: str
