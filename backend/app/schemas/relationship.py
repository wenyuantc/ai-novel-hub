"""关系类型、关系、关系历史与图谱的请求 / 响应模型。

语义：「a 是 b 的 role_a，b 是 a 的 role_b」。从 a 的视角对方显示为 role_b，从 b 的视角对方显示为 role_a。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.career import AssignmentOut, CareerAssignDraft
from app.schemas.story import CharacterCreate, CharacterOut

RelationCategory = Literal["family", "social", "hostile", "work", "other"]
RelationStatus = Literal["normal", "broken", "past", "complicated"]
# 接口可写入的来源（wizard / import / analysis 只由服务端内部写入）
ApiRelationSource = Literal["manual", "ai_parse"]


class RelationshipTypeCreate(BaseModel):
    category: RelationCategory = "other"
    role_a: str = Field(min_length=1, max_length=20)
    role_b: str = Field(min_length=1, max_length=20)


class RelationshipTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int | None
    category: str
    role_a: str
    role_b: str
    sort: int
    # 预置类型只读
    builtin: bool = False


class TypeRef(BaseModel):
    """指定关系类型：给 type_id，或给 role_a + role_b（匹配预置 / 自定义类型，匹配不上新建自定义类型）。"""

    type_id: int | None = None
    role_a: str | None = Field(default=None, max_length=20)
    role_b: str | None = Field(default=None, max_length=20)
    category: RelationCategory = "other"


class RelationshipCreate(TypeRef):
    a_id: int
    b_id: int
    intimacy: int = Field(default=0, ge=-100, le=100)
    status: RelationStatus = "normal"
    description: str = ""
    source: ApiRelationSource = "manual"
    chapter_no: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=200)


class RelationshipBatchCreate(BaseModel):
    items: list[RelationshipCreate] = Field(min_length=1, max_length=200)


class RelationshipUpdate(BaseModel):
    """省略 = 不变。a_id 用于调换方向（必须是这对角色之一，另一方自动成为 b）。"""

    a_id: int | None = None
    type_id: int | None = None
    intimacy: int | None = Field(default=None, ge=-100, le=100)
    status: RelationStatus | None = None
    description: str | None = None
    chapter_no: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=200)


class RelationshipOut(BaseModel):
    id: int
    project_id: int
    a_id: int
    b_id: int
    a_name: str
    b_name: str
    type_id: int
    category: str
    role_a: str
    role_b: str
    intimacy: int
    status: str
    description: str
    created_at: datetime
    updated_at: datetime
    # 仅在按 character_id 查询时有值：viewer 视角下的对方与称呼
    other_id: int | None = None
    other_name: str | None = None
    # 对方在 viewer 眼中的身份（a 视角为 role_b，b 视角为 role_a），如「徒弟」
    other_role: str | None = None
    # viewer 自己的身份，如「师父」
    self_role: str | None = None


class RelationshipHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    relationship_id: int
    chapter_no: int | None
    source: str
    changes: dict[str, Any]
    note: str
    created_at: datetime


class RelationDraft(TypeRef):
    """随新角色一起保存的关系：subject = 新角色。subject_is_a 为真时新角色是 a（role_a 一方）。"""

    target_id: int
    subject_is_a: bool = True
    intimacy: int = Field(default=0, ge=-100, le=100)
    status: RelationStatus = "normal"
    description: str = ""


class CharacterWithRelations(BaseModel):
    character: CharacterCreate
    relations: list[RelationDraft] = Field(default_factory=list, max_length=100)
    # 勾选的职业挂接（经 career_service.apply_assignment 写入，来源 ai）
    careers: list[CareerAssignDraft] = Field(default_factory=list, max_length=4)


class CharacterWithRelationsOut(BaseModel):
    character: CharacterOut
    relationships: list[RelationshipOut]
    careers: list[AssignmentOut] = Field(default_factory=list)


class GraphNode(BaseModel):
    id: int
    name: str
    aliases: list[str]
    role_type: str
    is_protagonist: bool
    life_status: str
    organization_ids: list[int]


class GraphEdge(BaseModel):
    id: int
    source: int
    target: int
    type_id: int
    category: str
    role_a: str
    role_b: str
    # 对称类型为 role_a，否则「role_a/role_b」
    label: str
    intimacy: int
    status: str
    description: str


class GraphOrganization(BaseModel):
    id: int
    name: str
    parent_id: int | None
    org_type: str


class RelationshipGraph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    organizations: list[GraphOrganization]
