"""项目备份的表规则注册表（ainovel.project）。

每张与项目相关的表都必须在这里登记为 TableRule，或者写进 NOT_EXPORTED 并说明理由；
已登记表的每个外键列、每个 JSON 列都必须声明处理方式。`tests/test_project_transfer.py::test_registry_*`
用反射校验这三点——以后新增表或列忘了登记，测试会直接失败。

列的处理方式（互斥，覆盖表的全部列）：
- `id`：导出原值，导入时由数据库分配新 id 并记入「旧 id → 新 id」映射。
- fill：不导出，导入时填当前值（`project` = 新项目 id，`user` = 导入者 id）。
- fk：导出原值，导入时按被引用表的映射改写；被引用表未随备份导出（可选项未勾选）时导出为 null。
  deferred 中的列先插入 null，全部表插入后再回填（自引用 / 互相引用）。
- custom：由 importer / exporter 中该表的专门逻辑处理（风格 ref、全局关系类型等）。
- exclude：不导出，导入时取默认值 / 重新计算（理由写在注释里）。
- 其余列原样导出导入；其中 JSON 列必须出现在 json_refs（含 id，需要重映射）或 json_plain（不含 id）中，
  str_refs 为含 id 的字符串列。
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import ColumnElement, or_, select

from app.models import (
    AnalysisChange,
    Base,
    Career,
    CareerHistory,
    CareerStage,
    Chapter,
    ChapterAnalysis,
    ChapterCharacter,
    ChapterPlan,
    ChapterVersion,
    Character,
    CharacterCareer,
    Foreshadow,
    ForeshadowCharacter,
    ForeshadowHistory,
    GenerationLog,
    InspirationSession,
    KbDocument,
    KbSettings,
    Organization,
    OrganizationMember,
    Outline,
    OutlineSnapshot,
    Project,
    Relationship,
    RelationshipHistory,
    RelationshipType,
    StoryMemory,
    WorldSetting,
)
from app.services.project_transfer import remap

Option = Literal["versions", "snapshots", "generation_logs", "inspiration"]
OPTIONS: tuple[Option, ...] = ("versions", "snapshots", "generation_logs", "inspiration")

# 与项目相关、但不导出的表（理由见值）。
# book_imports（拆书解析记录）只引用 users、与任何项目都无关，不在这张注册表的范围内（反射测试也不会把它算进来）
NOT_EXPORTED: dict[str, str] = {
    "agent_conversations": "智能体会话属于运行时数据，不跨实例迁移",
    "agent_messages": "智能体消息属于运行时数据，不跨实例迁移",
    "agent_changes": "智能体提案和效果属于运行时数据，不跨实例迁移",
    "tasks": "后台任务是运行时状态，导入后没有意义；导入后按需重新排队建索引",
    "kb_chunks": "由知识库文档的 text 重新分块得到，导入时在同一事务里重新生成",
    "retrieval_chunks": "检索单元（关键词 / 向量）是派生数据，导入时经业务写入点重建关键词部分，向量由重建任务生成",
}


@dataclass(frozen=True)
class TableRule:
    table: str
    model: type[Base]
    # 选出属于项目 pid 的行（导出用）
    scope: Callable[[int], ColumnElement[bool]]
    option: Option | None = None
    fill: dict[str, Literal["project", "user"]] = field(default_factory=dict)
    fk: dict[str, str] = field(default_factory=dict)
    deferred: frozenset[str] = frozenset()
    custom: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    json_refs: dict[str, remap.JsonRemap] = field(default_factory=dict)
    json_plain: tuple[str, ...] = ()
    str_refs: dict[str, Callable[[Any, remap.RemapContext], Any]] = field(default_factory=dict)

    @property
    def has_id(self) -> bool:
        return "id" in self.model.__table__.columns

    @property
    def columns(self) -> list[str]:
        """导出的普通列（含 id、fk、json），不含 fill / custom / exclude。"""
        skip = set(self.fill) | set(self.custom) | set(self.exclude)
        return [c.name for c in self.model.__table__.columns if c.name not in skip]


def _in(column, model, pid: int):
    return column.in_(select(model.id).where(model.project_id == pid))


def _own(model) -> Callable[[int], ColumnElement[bool]]:
    return lambda pid: model.project_id == pid


_P = {"project_id": "project"}

PROJECT_RULE = TableRule(
    table="projects",
    model=Project,
    scope=lambda pid: Project.id == pid,
    fill={"user_id": "user"},
    # default_style_id 经 styles[] 的 ref 映射（default_style_ref）
    custom=("default_style_id",),
    # 旧的写作风格自由文本：迁移 0013 起不再读写
    exclude=("style",),
    json_plain=("wizard_progress", "tags"),
    # 含章节 id；项目先于章节插入，importer 在全部表插入后再改写这一列
    json_refs={"reading_progress": remap.reading_progress},
)

# 导入顺序：被引用的表在前（自引用 / 互相引用的列放在 deferred 里回填）
RULES: tuple[TableRule, ...] = (
    TableRule("world_settings", WorldSetting, _own(WorldSetting), fill=_P),
    TableRule("characters", Character, _own(Character), fill=_P, json_plain=("aliases",)),
    TableRule(
        "organizations",
        Organization,
        _own(Organization),
        fill=_P,
        fk={"parent_id": "organizations"},
        deferred=frozenset({"parent_id"}),
    ),
    TableRule(
        "organization_members",
        OrganizationMember,
        lambda pid: _in(OrganizationMember.organization_id, Organization, pid),
        fk={"organization_id": "organizations", "character_id": "characters"},
    ),
    TableRule(
        "relationship_types",
        RelationshipType,
        # 本项目自定义类型 + 本项目关系用到的全局预置类型（导出为 global=true，导入时按 role_a / role_b 对应）
        lambda pid: or_(
            RelationshipType.project_id == pid,
            RelationshipType.id.in_(select(Relationship.type_id).where(Relationship.project_id == pid)),
        ),
        custom=("project_id",),
    ),
    TableRule(
        "relationships",
        Relationship,
        _own(Relationship),
        fill=_P,
        fk={"a_id": "characters", "b_id": "characters", "type_id": "relationship_types"},
        # 由 a_id / b_id 重新计算
        exclude=("pair_low", "pair_high"),
    ),
    TableRule(
        "relationship_history",
        RelationshipHistory,
        lambda pid: _in(RelationshipHistory.relationship_id, Relationship, pid),
        fk={"relationship_id": "relationships"},
        # 类型 / 方向以名称记录（relationship_service.apply_change），不含 id
        json_plain=("changes",),
    ),
    TableRule("careers", Career, _own(Career), fill=_P),
    TableRule(
        "career_stages",
        CareerStage,
        lambda pid: _in(CareerStage.career_id, Career, pid),
        fk={"career_id": "careers"},
    ),
    TableRule(
        "character_careers",
        CharacterCareer,
        lambda pid: _in(CharacterCareer.character_id, Character, pid),
        fk={"character_id": "characters", "career_id": "careers", "stage_id": "career_stages"},
    ),
    TableRule(
        "career_history",
        CareerHistory,
        lambda pid: _in(CareerHistory.character_id, Character, pid),
        fk={"character_id": "characters", "career_id": "careers"},
        # 阶段以「第N阶·名称」记录（career_service.apply_assignment），不含 id
        json_plain=("changes",),
    ),
    TableRule("outlines", Outline, _own(Outline), fill=_P),
    TableRule(
        "chapters",
        Chapter,
        _own(Chapter),
        fill=_P,
        fk={"outline_id": "outlines", "plan_id": "chapter_plans"},
        deferred=frozenset({"plan_id"}),
        # 经 styles[] 的 ref 映射（style_ref）
        custom=("style_id",),
    ),
    TableRule(
        "chapter_plans",
        ChapterPlan,
        _own(ChapterPlan),
        fill=_P,
        fk={"outline_id": "outlines", "chapter_id": "chapters"},
        json_refs={"character_ids": remap.id_list("characters")},
        json_plain=("key_events",),
    ),
    TableRule(
        "chapter_characters",
        ChapterCharacter,
        lambda pid: _in(ChapterCharacter.chapter_id, Chapter, pid),
        fk={"chapter_id": "chapters", "character_id": "characters"},
    ),
    TableRule("foreshadows", Foreshadow, _own(Foreshadow), fill=_P),
    TableRule(
        "foreshadow_characters",
        ForeshadowCharacter,
        lambda pid: _in(ForeshadowCharacter.foreshadow_id, Foreshadow, pid),
        fk={"foreshadow_id": "foreshadows", "character_id": "characters"},
    ),
    TableRule(
        "foreshadow_history",
        ForeshadowHistory,
        lambda pid: _in(ForeshadowHistory.foreshadow_id, Foreshadow, pid),
        fk={"foreshadow_id": "foreshadows"},
        # 关联角色以名字列表记录（foreshadow_service.apply_foreshadow），不含 id
        json_plain=("changes",),
    ),
    TableRule(
        "generation_logs",
        GenerationLog,
        _own(GenerationLog),
        option="generation_logs",
        fill=_P,
        fk={"chapter_id": "chapters"},
        # 配置 / 任务属于导出者，不随项目迁移（provider / model 已冗余存储）
        exclude=("ai_config_id", "task_id"),
        # params / messages 只作信息记录，原样保留
        json_plain=("params", "messages"),
    ),
    TableRule(
        "chapter_versions",
        ChapterVersion,
        lambda pid: _in(ChapterVersion.chapter_id, Chapter, pid),
        option="versions",
        fk={"chapter_id": "chapters", "generation_log_id": "generation_logs"},
    ),
    TableRule(
        "chapter_analyses",
        ChapterAnalysis,
        _own(ChapterAnalysis),
        fill=_P,
        fk={"chapter_id": "chapters"},
        # 任务不导出
        exclude=("task_id",),
        json_refs={"foreshadow_candidates": remap.foreshadow_candidates},
        # changes_raw 按名字记录模型输出的变化，其余为分析结果本身，均不含 id
        json_plain=(
            "plot_points",
            "hooks",
            "conflict",
            "emotion",
            "changes_raw",
            "scores",
            "metrics",
            "suggestions",
            "warnings",
        ),
    ),
    TableRule(
        "analysis_changes",
        AnalysisChange,
        _own(AnalysisChange),
        fill=_P,
        fk={"analysis_id": "chapter_analyses"},
        json_refs={
            "target": remap.analysis_payload,
            "before": remap.analysis_payload,
            "after": remap.analysis_payload,
        },
        str_refs={"field_key": remap.field_key},
    ),
    TableRule(
        "story_memories",
        StoryMemory,
        _own(StoryMemory),
        fill=_P,
        fk={"chapter_id": "chapters", "analysis_id": "chapter_analyses"},
        # 相关角色名
        json_plain=("characters",),
    ),
    TableRule("kb_settings", KbSettings, _own(KbSettings), fill=_P),
    TableRule(
        "kb_documents",
        KbDocument,
        _own(KbDocument),
        fill=_P,
        # 导入时重新分块：状态与分块数重新计算
        exclude=("status", "chunk_count"),
        json_plain=("warnings",),
    ),
    TableRule(
        "outline_snapshots",
        OutlineSnapshot,
        _own(OutlineSnapshot),
        option="snapshots",
        fill=_P,
        json_refs={"data": remap.snapshot_data},
    ),
    TableRule(
        "inspiration_sessions",
        InspirationSession,
        # 从本项目发起，或已转为本项目的会话
        lambda pid: or_(InspirationSession.project_id == pid, InspirationSession.converted_project_id == pid),
        option="inspiration",
        fill={"user_id": "user"},
        # 都指向新项目（指向其他项目的置空）
        fk={"project_id": "projects", "converted_project_id": "projects"},
        # 结构见 schemas.inspiration.SessionState，只有文本，不含 id
        json_plain=("state",),
    ),
)

RULES_BY_TABLE: dict[str, TableRule] = {r.table: r for r in RULES}
TABLE_NAMES: tuple[str, ...] = tuple(r.table for r in RULES)
