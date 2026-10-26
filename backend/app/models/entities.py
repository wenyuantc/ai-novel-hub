"""核心表定义。

不声明 ORM relationship：级联删除由数据库外键 ON DELETE 处理，
所有访问都经 service 层显式查询并按 user_id 过滤。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

CHAPTER_STATUSES = ("draft", "generating", "done")
AI_PROVIDERS = ("openai_compat", "anthropic", "gemini")
PROJECT_STATUSES = ("planning", "writing", "completed")
# 版本来源：手动保存 / AI 或回滚替换前补的快照 / 整章生成 / 润色 / 局部改写采纳 / 回滚
VERSION_SOURCES = ("manual", "baseline", "generate", "polish", "rewrite", "restore")
# 生成记录类型：整章生成、润色，以及局部改写的各个操作
GENERATION_KINDS = ("generate", "polish", "rewrite", "expand", "condense", "deslop", "continue")
GENERATION_STATUSES = ("success", "error", "cancelled")
# 后台任务
TASK_KINDS = (
    "chapter.generate",
    "chapter.polish",
    "wizard.outline",
    "wizard.characters",
    "wizard.world",
    "character.generate",
    "organization.generate",
    "relations.parse",
    "career.generate",
    "wizard.careers",
)
TASK_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled", "interrupted")
ACTIVE_TASK_STATUSES = ("queued", "running")
FINISHED_TASK_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")
# 结构化设定
LIFE_STATUSES = ("alive", "dead", "missing", "other")
WORLD_CATEGORIES = ("place", "faction", "item", "rule", "history", "other")
MEMBER_STATUSES = ("active", "left", "expelled", "dead")
RELATION_CATEGORIES = ("family", "social", "hostile", "work", "other")
RELATION_STATUSES = ("normal", "broken", "past", "complicated")
# 关系历史来源：手动 / 角色向导 / AI 解析旧备注 / 卡片导入 / 章节分析（analysis-memory）
RELATION_SOURCES = ("manual", "wizard", "ai_parse", "import", "analysis")
# 职业：主修（只能挂为主职业）/ 副修（只能挂为副职业）
CAREER_KINDS = ("main", "sub")
# 职业挂接历史来源：手动 / 向导 / AI 生成角色 / 卡片导入 / 章节分析（analysis-memory）
CAREER_SOURCES = ("manual", "wizard", "ai", "import", "analysis")
# 全局预置关系类型 (category, role_a, role_b)：「a 是 b 的 role_a，b 是 a 的 role_b」。
# 迁移 0007 内有同样的一份（迁移脚本冻结，不引用应用代码）；create_all 时由 after_create 事件写入
PRESET_RELATIONSHIP_TYPES = (
    ("family", "父亲", "子女"),
    ("family", "母亲", "子女"),
    ("family", "兄长", "弟妹"),
    ("family", "姐姐", "弟妹"),
    ("family", "配偶", "配偶"),
    ("family", "亲属", "亲属"),
    ("social", "师父", "徒弟"),
    ("social", "朋友", "朋友"),
    ("social", "知己", "知己"),
    ("social", "恋人", "恋人"),
    ("social", "同门", "同门"),
    ("social", "恩人", "受恩者"),
    ("hostile", "仇人", "仇人"),
    ("hostile", "宿敌", "宿敌"),
    ("hostile", "竞争对手", "竞争对手"),
    ("work", "上司", "下属"),
    ("work", "同僚", "同僚"),
    ("work", "合作者", "合作者"),
    ("work", "主人", "仆从"),
    ("other", "相识", "相识"),
)
# 部分唯一索引条件：同一资源同时只能有一个排队 / 运行中的任务（PostgreSQL 与 SQLite 都支持）
_ACTIVE_RESOURCE_WHERE = "status IN ('queued', 'running') AND resource_key IS NOT NULL"


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    # 会话版本号：重置密码 / 禁用 / 自助改密时递增，使旧令牌（payload.ver）立即失效
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class AIConfig(TimestampMixin, Base):
    __tablename__ = "ai_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    # text / image；默认配置按类型各自唯一
    model_type: Mapped[str] = mapped_column(String(16), default="text", server_default="text", nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # off / low / medium / high；关闭 = 不发送任何思考参数
    thinking_level: Mapped[str] = mapped_column(String(16), default="off", server_default="off", nullable=False)
    # 单位 K（×1000 tokens）；为空表示使用系统 / 接口默认
    context_window_k: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_k: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    genre: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    premise: Mapped[str] = mapped_column(Text, default="", nullable=False)
    style: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="planning", nullable=False)


class WorldSetting(TimestampMixin, Base):
    __tablename__ = "world_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # place / faction / item / rule / history / other
    category: Mapped[str] = mapped_column(String(16), default="other", server_default="other", nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Character(TimestampMixin, Base):
    __tablename__ = "characters"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 下拉选项（主角 / 重要配角 / 配角 / 反派 / 其他），兼容旧的自由文本值
    role_type: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    # 简介（存量自由文本，原样保留）
    profile: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 旧关系备注：只读保留，可经 relations.parse 解析为结构化关系
    relations_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]", nullable=False)
    age: Mapped[str] = mapped_column(String(50), default="", server_default="", nullable=False)
    gender: Mapped[str] = mapped_column(String(20), default="", server_default="", nullable=False)
    appearance: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    personality: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    background: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # 当前处境；以后由 analysis-memory 回写
    current_state: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    life_status: Mapped[str] = mapped_column(String(16), default="alive", server_default="alive", nullable=False)
    always_in_context: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    org_type: Mapped[str] = mapped_column(String(50), default="", server_default="", nullable=False)
    purpose: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    location: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    # 0–100，可空
    power_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 上级组织被删除时下级保留（变为顶级）
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), index=True, nullable=True
    )
    description: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class OrganizationMember(TimestampMixin, Base):
    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("organization_id", "character_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), index=True, nullable=False
    )
    position: Mapped[str] = mapped_column(String(50), default="", server_default="", nullable=False)
    # active / left / expelled / dead
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)


class RelationshipType(TimestampMixin, Base):
    """关系类型：「a 是 b 的 role_a，b 是 a 的 role_b」；对称类型两边相同。project_id 为空 = 全局预置（只读）。"""

    __tablename__ = "relationship_types"
    __table_args__ = (UniqueConstraint("project_id", "role_a", "role_b"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    category: Mapped[str] = mapped_column(String(16), default="other", server_default="other", nullable=False)
    role_a: Mapped[str] = mapped_column(String(20), nullable=False)
    role_b: Mapped[str] = mapped_column(String(20), nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


@event.listens_for(RelationshipType.__table__, "after_create")
def _seed_relationship_types(target, connection, **_kw) -> None:
    """create_all（测试）建表后写入预置类型；生产环境由迁移 0007 写入。"""
    connection.execute(
        target.insert(),
        [
            {"project_id": None, "category": c, "role_a": a, "role_b": b, "sort": i}
            for i, (c, a, b) in enumerate(PRESET_RELATIONSHIP_TYPES, start=1)
        ],
    )


class Relationship(TimestampMixin, Base):
    """一对角色只存一条无方向记录：a 是 b 的 type.role_a，b 是 a 的 type.role_b。

    pair_low / pair_high = min / max(a_id, b_id)，由 relationship_service.apply_change 写入，保证按角色对唯一。
    """

    __tablename__ = "relationships"
    __table_args__ = (
        UniqueConstraint("pair_low", "pair_high"),
        CheckConstraint("a_id <> b_id", name="distinct_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    a_id: Mapped[int] = mapped_column(ForeignKey("characters.id", ondelete="CASCADE"), index=True, nullable=False)
    b_id: Mapped[int] = mapped_column(ForeignKey("characters.id", ondelete="CASCADE"), index=True, nullable=False)
    pair_low: Mapped[int] = mapped_column(Integer, nullable=False)
    pair_high: Mapped[int] = mapped_column(Integer, nullable=False)
    # 不设 ondelete（NO ACTION，语句结束时检查）：仍被引用的类型不能删除（服务层先查返回 409）；
    # 不用 RESTRICT，避免删除项目时级联删除类型与关系的先后顺序导致失败
    type_id: Mapped[int] = mapped_column(ForeignKey("relationship_types.id"), index=True, nullable=False)
    # -100～100
    intimacy: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # normal / broken / past / complicated
    status: Mapped[str] = mapped_column(String(16), default="normal", server_default="normal", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)


class RelationshipHistory(TimestampMixin, Base):
    """关系的每次变化（含新建）。changes = {字段: [旧, 新]}，类型 / 方向以名称记录，便于回看。"""

    __tablename__ = "relationship_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    relationship_id: Mapped[int] = mapped_column(
        ForeignKey("relationships.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chapter_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    note: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)


class ChapterCharacter(TimestampMixin, Base):
    """章节手动指定的出场角色。"""

    __tablename__ = "chapter_characters"

    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), primary_key=True)
    character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Career(TimestampMixin, Base):
    """职业定义。kind 决定只能挂为主职业（main）还是副职业（sub）。"""

    __tablename__ = "careers"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # main / sub
    kind: Mapped[str] = mapped_column(String(8), default="main", server_default="main", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # 获得条件
    requirements: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # 特殊能力
    abilities: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # 体系规则
    rules: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class CareerStage(TimestampMixin, Base):
    """职业阶段（有序阶梯）。position 由 career_service 维护为从 1 开始连续；不建唯一约束，便于整体重排。

    id 稳定：重排只改 position，挂接引用 stage_id，因此角色所处的那一阶不随顺序变化。
    """

    __tablename__ = "career_stages"

    id: Mapped[int] = mapped_column(primary_key=True)
    career_id: Mapped[int] = mapped_column(ForeignKey("careers.id", ondelete="CASCADE"), index=True, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)


class CharacterCareer(TimestampMixin, Base):
    """角色挂接的职业与当前阶段。只能经 career_service.apply_assignment 写入（写历史）。"""

    __tablename__ = "character_careers"
    __table_args__ = (UniqueConstraint("character_id", "career_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), index=True, nullable=False
    )
    career_id: Mapped[int] = mapped_column(ForeignKey("careers.id", ondelete="CASCADE"), index=True, nullable=False)
    # main / sub，恒等于职业的 kind
    role: Mapped[str] = mapped_column(String(8), nullable=False)
    # 不设 ondelete（NO ACTION）：仍被引用的阶段不能删除（服务层先查返回 409）；
    # 删除职业时阶段与挂接在同一语句内级联删除，与 relationships.type_id 同理
    stage_id: Mapped[int] = mapped_column(ForeignKey("career_stages.id"), index=True, nullable=False)
    # 状态备注，如「半步金丹」「瓶颈期」
    note: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)


class CareerHistory(TimestampMixin, Base):
    """挂接的每次变化（新挂 / 换阶 / 改备注 / 主副变化 / 卸下）。

    changes = {字段: [旧, 新]}，阶段以「第N阶·名称」记录；career_name 冗余保存。删除职业时历史随之级联删除。
    """

    __tablename__ = "career_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), index=True, nullable=False
    )
    career_id: Mapped[int | None] = mapped_column(
        ForeignKey("careers.id", ondelete="CASCADE"), index=True, nullable=True
    )
    career_name: Mapped[str] = mapped_column(String(100), default="", server_default="", nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    chapter_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    note: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)


class Outline(TimestampMixin, Base):
    __tablename__ = "outlines"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    volume_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    volume_title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    chapter_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Chapter(TimestampMixin, Base):
    __tablename__ = "chapters"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    # 大纲被重新生成/删除时章节正文必须保留，因此用 SET NULL 而非 CASCADE
    outline_id: Mapped[int | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"), index=True, nullable=True
    )
    chapter_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class GenerationLog(TimestampMixin, Base):
    """一次 AI 调用的记录。provider / model 冗余存储，配置删除后仍可查看。"""

    __tablename__ = "generation_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    # 章节删除后记录保留在项目下
    chapter_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    ai_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_configs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    provider: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    output_chars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 预留：现有 provider 流式接口拿不到 token 用量，暂时为空
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 产生该调用的后台任务；任务被清理后记录保留
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True)


class ChapterVersion(TimestampMixin, Base):
    """章节正文快照（事件发生后的状态）。合并手动保存时会刷新 updated_at。"""

    __tablename__ = "chapter_versions"
    __table_args__ = (UniqueConstraint("chapter_id", "seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True, nullable=False)
    # 章节内递增序号（max+1），删除后不复用
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    starred: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    note: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    generation_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("generation_logs.id", ondelete="SET NULL"), index=True, nullable=True
    )


class Task(TimestampMixin, Base):
    """持久化后台任务。tasks 表即队列：进程内调度器按 id 先进先出领取 queued 任务。"""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_status_id", "status", "id"),
        Index("ix_tasks_user_id_id", "user_id", "id"),
        Index(
            "uq_tasks_active_resource",
            "resource_key",
            unique=True,
            postgresql_where=text(_ACTIVE_RESOURCE_WHERE),
            sqlite_where=text(_ACTIVE_RESOURCE_WHERE),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # (user_id, id) 复合索引已覆盖按用户查询，不再单独建索引
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    chapter_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # 展示用中文标题（创建时生成，章节被删 / 改名后保持不变）
    title: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    # chapter:{id} / wizard:{project_id}:{kind}；用于同资源互斥
    resource_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_text: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    stage: Mapped[str] = mapped_column(String(100), default="", server_default="", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # 多步任务断点（当前单步类型不用）
    step_cursor: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    retry_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
