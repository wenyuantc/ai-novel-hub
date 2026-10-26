"""批量顺序生成（chapter.batch）的请求与预览。"""

from typing import Literal

from pydantic import BaseModel, Field


class BatchGenerateRequest(BaseModel):
    # 范围按章节号确定（两端都包含），发起时按 (chapter_no, id) 定下章节 id 列表
    start_chapter_id: int
    end_chapter_id: int
    # 包含已写章节：重写并覆盖（沿用版本规则，覆盖前存 baseline）
    include_written: bool = False
    # 统一字数：没有细纲（或不使用细纲字数）的章节使用
    target_words: int = Field(default=3000, ge=300, le=20000)
    # 章节有细纲时用细纲的 est_words；关闭 = 统一字数覆盖全部章节
    use_plan_words: bool = True
    auto_continue: bool = True
    # 0 = 不暂停；N = 每生成 N 章进入「待审核」
    review_every: int = Field(default=0, ge=0, le=50)
    # 单章重试仍失败时：false = 任务失败并停在该章；true = 跳过该章继续
    skip_on_fail: bool = False
    instruction: str = Field(default="", max_length=2000)
    ai_config_id: int | None = None


class BatchPreviewItem(BaseModel):
    chapter_id: int
    chapter_no: int
    title: str
    target_words: int
    words_source: Literal["plan", "uniform"]
    # 已写章节（勾选包含已写时会被重写）
    written: bool


class BatchSkippedItem(BaseModel):
    chapter_id: int
    chapter_no: int
    title: str
    word_count: int


class BatchPreview(BaseModel):
    total: int
    items: list[BatchPreviewItem]
    skipped: list[BatchSkippedItem]
    analyze: bool
    estimated_minutes: int
    warnings: list[str]
