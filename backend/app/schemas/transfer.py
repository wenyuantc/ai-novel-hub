"""项目备份（ainovel.project）与导入的请求 / 响应结构。"""

from pydantic import BaseModel, Field

from app.schemas.project import ProjectOut


class BackupOptions(BaseModel):
    versions: bool = True
    snapshots: bool = False
    generation_logs: bool = False
    inspiration: bool = False


class EstimateGroup(BaseModel):
    rows: int = 0
    chars: int = 0
    # 粗略估计的文件字节数（中文按 UTF-8 每字 3 字节，加上每行的键名开销）
    bytes: int = 0


class BackupEstimate(BaseModel):
    core: EstimateGroup
    versions: EstimateGroup
    snapshots: EstimateGroup
    generation_logs: EstimateGroup
    inspiration: EstimateGroup


class ImportStyleBrief(BaseModel):
    name: str
    preset_key: str | None = None
    # preset = 对应到预设；reuse = 复用同名同内容的风格；create = 新建（name 为导入后的名称）；
    # copy = 预设缺失，复制为自定义风格
    action: str


class ImportPreview(BaseModel):
    token: str
    title: str
    # 实际导入时使用的书名（重名时加「（导入）」）
    final_title: str
    version: int
    exported_at: str | None = None
    app_schema: str | None = None
    options: BackupOptions
    counts: dict[str, int]
    styles: list[ImportStyleBrief] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # 令牌有效期（秒）
    expires_in: int


class ImportRequest(BaseModel):
    token: str = Field(min_length=1, max_length=100)
    title: str | None = Field(default=None, max_length=200)


class ImportResult(BaseModel):
    project: ProjectOut
    counts: dict[str, int]
    warnings: list[str] = Field(default_factory=list)
    # 有向量模型时排队的重建索引任务
    task_id: int | None = None
