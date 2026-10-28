from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.style import StyleChoice

ChapterStatus = Literal["draft", "generating", "done"]


class ChapterCreate(BaseModel):
    outline_id: int | None = None
    chapter_no: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=200)
    content: str = ""


class ChapterUpdate(BaseModel):
    outline_id: int | None = None
    # 本章写作风格覆盖；显式 null = 继承项目默认
    style_id: int | None = None
    chapter_no: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=200)
    content: str | None = None
    # 不允许手动设为 generating；可用于把卡住的 generating 章节复位
    status: Literal["draft", "done"] | None = None


class ChaptersFromOutlines(BaseModel):
    # 为空表示为项目中所有尚未建章的大纲条目创建章节
    outline_ids: list[int] | None = None


class ChapterBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    outline_id: int | None
    # 对应的细纲（outline-pro）；未按细纲建章时为空
    plan_id: int | None = None
    # 本章写作风格覆盖（为空 = 继承项目默认）
    style_id: int | None = None
    chapter_no: int
    title: str
    status: ChapterStatus
    word_count: int
    updated_at: datetime


class ChapterOut(ChapterBrief):
    content: str


class GenerateChapterRequest(BaseModel):
    ai_config_id: int | None = None
    instruction: str = Field(default="", max_length=2000)
    target_words: int = Field(default=3000, ge=300, le=20000)
    # 字数低于目标 85% 时自动续写（最多 2 轮）。接口默认关闭以兼容旧客户端，编辑器默认显式开启
    auto_continue: bool = False
    # 写作风格：inherit = 章节覆盖 > 项目默认；none = 不用风格；整数 = 临时指定风格 id
    style: StyleChoice = "inherit"
    # 选用的 Skill（本人或全局）作为补充工作流；任务创建时快照正文，之后编辑 Skill 不影响重试
    skill_id: int | None = Field(default=None, gt=0)


class PolishChapterRequest(BaseModel):
    ai_config_id: int | None = None
    instruction: str = Field(default="", max_length=2000)
    # 写作风格：inherit = 章节覆盖 > 项目默认；none = 不用风格；整数 = 临时指定风格 id
    style: StyleChoice = "inherit"
    # 选用的 Skill（本人或全局）作为补充工作流；任务创建时快照正文，之后编辑 Skill 不影响重试
    skill_id: int | None = Field(default=None, gt=0)


class WizardRequest(BaseModel):
    ai_config_id: int | None = None
    instruction: str = Field(default="", max_length=2000)
    # 仅 outline 使用：replace = 覆盖（覆盖前自动存快照）；append = 追加到末尾（从下一卷、下一章接着写）
    outline_mode: Literal["replace", "append"] = "replace"
    volume_count: int = Field(default=2, ge=1, le=20)
    chapters_per_volume: int = Field(default=10, ge=1, le=100)
    # 仅 characters 使用
    character_count: int = Field(default=5, ge=3, le=30)
    # 仅 careers 使用
    career_count: int = Field(default=4, ge=1, le=12)
    # 从中断处继续：引用同项目同类型、失败 / 已取消 / 已中断且有部分输出的向导任务；
    # 生成参数沿用该任务的原请求（只有 ai_config_id 可以换）
    continue_from_task_id: int | None = None


class ReviseChapterRequest(BaseModel):
    """按分析建议整章重写（chapter.revise）。至少勾选一条建议或填写 instruction。"""

    analysis_id: int
    suggestion_ids: list[int] = Field(default_factory=list, max_length=20)
    instruction: str = Field(default="", max_length=2000)
    # 需保留的要素：预置「保持整体结构 / 保持人物性格 / 保留关键对白」或自定义
    preserve: list[str] = Field(default_factory=list, max_length=10)
    # 为空 = 原文字数
    target_words: int | None = Field(default=None, ge=300, le=20000)
    ai_config_id: int | None = None
    # 写作风格：inherit = 章节覆盖 > 项目默认；none = 不用风格；整数 = 临时指定风格 id
    style: StyleChoice = "inherit"
    # 选用的 Skill（本人或全局）作为补充工作流；任务创建时快照正文，之后编辑 Skill 不影响重试
    skill_id: int | None = Field(default=None, gt=0)
