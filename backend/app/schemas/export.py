"""站内可阅读文档导出的严格选择契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.setting_field_value import EntityRef, PositiveDbInt


class DocumentExportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StorySelection(DocumentExportModel):
    scope: Literal["all", "selected", "none"] = "all"
    unit: Literal["chapter", "scene"] = "chapter"
    content: Literal["prose", "summary", "both"] = "prose"
    ids: list[PositiveDbInt] = Field(default_factory=list)

    @model_validator(mode="after")
    def selection_contract(self):
        if len(self.ids) != len(set(self.ids)):
            raise ValueError("选中的章节或场景不能重复")
        if self.scope == "selected" and not self.ids:
            raise ValueError("请选择章节或场景")
        if self.scope != "selected" and self.ids:
            raise ValueError("全部或不包含正文时不能指定章节或场景")
        return self


class MaterialSelection(DocumentExportModel):
    settings: list[EntityRef] = Field(default_factory=list)
    conversation_ids: list[PositiveDbInt] = Field(default_factory=list)
    note_ids: list[PositiveDbInt] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.settings)) != len(self.settings):
            raise ValueError("设定条目不能重复")
        if len(set(self.conversation_ids)) != len(self.conversation_ids):
            raise ValueError("聊天会话不能重复")
        if len(set(self.note_ids)) != len(self.note_ids):
            raise ValueError("笔记不能重复")
        return self


class DocumentExportRequest(DocumentExportModel):
    format: Literal["docx", "md", "txt"]
    story: StorySelection = Field(default_factory=StorySelection)
    materials: MaterialSelection = Field(default_factory=MaterialSelection)
