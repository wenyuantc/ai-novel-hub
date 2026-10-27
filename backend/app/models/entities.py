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
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
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
from app.models.style_presets import PRESET_STYLES

CHAPTER_STATUSES = ("draft", "generating", "done")
AI_PROVIDERS = ("openai_compat", "anthropic", "gemini")
PROJECT_STATUSES = ("planning", "writing", "completed")
# 版本来源：手动保存 / AI 或回滚替换前补的快照 / 整章生成 / 润色 / 局部改写采纳 / 回滚 / 按建议重写
VERSION_SOURCES = ("manual", "baseline", "generate", "polish", "rewrite", "restore", "revise")
# 生成记录类型：整章生成、润色、按建议重写，以及局部改写的各个操作
GENERATION_KINDS = ("generate", "polish", "rewrite", "expand", "condense", "deslop", "continue", "revise")
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
    "chapter.analyze",
    "chapter.revise",
    "outline.continue",
    "outline.expand",
    "chapter.batch",
    "kb.index",
)
TASK_STATUSES = ("queued", "running", "paused", "succeeded", "failed", "cancelled", "interrupted")
# 活动：占资源键（部分唯一索引）、由调度器领取
ACTIVE_TASK_STATUSES = ("queued", "running")
# 未结束：活动 + 待审核（paused 不占资源键与并发名额、不被调度，也不会被裁剪或删除）
OPEN_TASK_STATUSES = (*ACTIVE_TASK_STATUSES, "paused")
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
# 章节分析回写条目：类型与状态（analysis-memory）
ANALYSIS_CHANGE_KINDS = (
    "character_state",
    "life_status",
    "relationship_update",
    "relationship_create",
    "career_stage",
    "org_membership",
    "foreshadow_resolve",
    "foreshadow_plant_planned",
    "foreshadow_new",
)
ANALYSIS_CHANGE_STATUSES = ("applied", "pending", "reverted", "rejected", "skipped", "failed")
# 伏笔：待埋 / 已埋 / 已回收 / 部分回收 / 已废弃；来源：手动 / 章节分析确认创建（foreshadows）
FORESHADOW_STATUSES = ("pending", "planted", "resolved", "partial", "abandoned")
FORESHADOW_SOURCES = ("manual", "analysis")
# 大纲快照原因：向导覆盖 / 导入替换 / 重新展开 / 手动保存 / 恢复前（outline-pro）
OUTLINE_SNAPSHOT_REASONS = ("wizard_replace", "import_replace", "reexpand", "manual", "restore")
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
    # 旧的写作风格自由文本：迁移 0013 起不再读写（已迁移为项目的「原风格」），下一个大版本删除
    style: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="planning", nullable=False)
    # 项目默认写作风格（删除风格时 SET NULL，即回落为不用风格）
    default_style_id: Mapped[int | None] = mapped_column(
        ForeignKey("writing_styles.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # AI 整章生成 / 润色 / 按建议重写成功后自动排队分析本章
    auto_analyze: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    # 向导进度 {done: [步骤], skipped: [步骤], current: 步骤 | null}；重新进入向导时从中断处继续
    wizard_progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}", nullable=False)
    # 立项结构化字段（迁移 0014）：前五项非空时注入【作品信息】；target_words 只用于进度与向导参数建议
    synopsis: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    theme: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]", nullable=False)
    selling_points: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    audience: Mapped[str] = mapped_column(String(100), default="", server_default="", nullable=False)
    target_words: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 阅读进度（迁移 0016）{chapter_id, chapter_no, ratio: 0–1, updated_at}；
    # chapter_no 用于章节被删除后读取时退回前一已写章节
    reading_progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}", nullable=False)


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
    # 对应的细纲；与 chapter_plans.chapter_id 双向冗余，由 plan_service 维护一致。
    # use_alter：chapters 与 chapter_plans 互相引用，建表后再加外键
    plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapter_plans.id", ondelete="SET NULL", use_alter=True), index=True, nullable=True
    )
    # 本章写作风格覆盖；为空 = 继承项目默认（删除风格时 SET NULL，即回落到项目默认）
    style_id: Mapped[int | None] = mapped_column(
        ForeignKey("writing_styles.id", ondelete="SET NULL"), index=True, nullable=True
    )


class ChapterPlan(TimestampMixin, Base):
    """细纲：一条大纲展开成的多章规划（seq 从 1 开始）。已建章时 chapter_id 有值。

    只经 plan_service 写入；chapter_id 与 chapters.plan_id 双向一致。大纲删除时细纲级联删除，章节保留（plan_id 置空）。
    """

    __tablename__ = "chapter_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    outline_id: Mapped[int] = mapped_column(ForeignKey("outlines.id", ondelete="CASCADE"), index=True, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    key_events: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]", nullable=False)
    # 出场角色 id；读取时过滤已删除的角色，导出时转为名字
    character_ids: Mapped[list[int]] = mapped_column(JSON, default=list, server_default="[]", nullable=False)
    emotion: Mapped[str] = mapped_column(String(100), default="", server_default="", nullable=False)
    goal: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    est_words: Mapped[int] = mapped_column(Integer, default=3000, server_default="3000", nullable=False)
    chapter_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), unique=True, nullable=True
    )


class OutlineSnapshot(TimestampMixin, Base):
    """大纲快照：data = {outlines, plans, links}（原始 id）。每个项目保留最近 20 个。"""

    __tablename__ = "outline_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    # wizard_replace / import_replace / reexpand / manual / restore
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


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
    # 同键任务排队串行（不拒绝）：调度时同键已有运行中的任务则跳过，按 id 先进先出；如 analysis:{project_id}
    serial_key: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_text: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    stage: Mapped[str] = mapped_column(String(100), default="", server_default="", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # 多步任务断点：下一步的下标（如批量生成中下一章在 chapter_ids 中的位置）；单步类型恒为 0
    step_cursor: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # 多步任务的结构化断点（批量生成的阶段 / 当前章 / 每章结果）；重试与继续时连同 step_cursor 一起保留
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    retry_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChapterAnalysis(TimestampMixin, Base):
    """一次章节分析（每次一行，历史保留）。章节的最新分析 = max(id)；content_hash 与当前正文不同即已过期。"""

    __tablename__ = "chapter_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True, nullable=False)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True)
    # 分析时正文的 sha256
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 冗余：回写时比较章节先后
    chapter_no: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    plot_points: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    hooks: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    conflict: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    emotion: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # 模型输出的原始变化（规范化后），便于排查
    changes_raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # 原样保存，留给 foreshadows 使用
    foreshadow_candidates: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    # {overall, pacing, engagement, coherence}；overall 由服务端对三项求平均
    scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # 本地规则计算：{words, paragraphs, avg_paragraph_chars, dialogue_ratio}
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # [{id, category, priority, text}]
    suggestions: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    # 规范化时丢弃 / 截断的提示
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    model: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)


class AnalysisChange(TimestampMixin, Base):
    """分析回写条目：一次分析识别出的一项设定变化及其处理结果（已写入 / 待确认 / 已撤销 / 已拒绝 / 已跳过 / 失败）。"""

    __tablename__ = "analysis_changes"
    __table_args__ = (Index("ix_analysis_changes_project_id_field_key", "project_id", "field_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("chapter_analyses.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    chapter_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    target: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # 规范化字段标识（char:12:current_state / rel:5:intimacy / career:12:3 / org:4:12 / relpair:3:7），名字对不上时为空
    field_key: Mapped[str] = mapped_column(String(120), default="", server_default="", nullable=False)
    before: Mapped[Any] = mapped_column(JSON, nullable=True)
    after: Mapped[Any] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    reason: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Foreshadow(TimestampMixin, Base):
    """伏笔。四个章节字段存章节号（不做外键）：可指向尚未创建的章节，重排章节也不受影响。

    没有计划回收章的已埋 / 部分回收伏笔即长线伏笔（派生，不入库）。
    只能经 foreshadow_service.apply_foreshadow 写入（写历史）。
    """

    __tablename__ = "foreshadows"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # 埋法要点 / 回收要点
    plant_hint: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    reveal_hint: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # pending / planted / resolved / partial / abandoned
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending", nullable=False)
    planned_plant_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_plant_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    planned_resolve_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_resolve_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 1–5
    importance: Mapped[int] = mapped_column(Integer, default=3, server_default="3", nullable=False)
    include_in_context: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # manual / analysis（确认分析识别的新伏笔时创建）
    source: Mapped[str] = mapped_column(String(12), default="manual", server_default="manual", nullable=False)


class ForeshadowCharacter(TimestampMixin, Base):
    """伏笔关联的角色。删除角色时只删除关联行，伏笔保留。"""

    __tablename__ = "foreshadow_characters"

    foreshadow_id: Mapped[int] = mapped_column(ForeignKey("foreshadows.id", ondelete="CASCADE"), primary_key=True)
    character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class ForeshadowHistory(TimestampMixin, Base):
    """伏笔的每次变化（含新建）。changes = {字段: [旧, 新]}，关联角色以名字列表记录在 characters 键。"""

    __tablename__ = "foreshadow_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    foreshadow_id: Mapped[int] = mapped_column(
        ForeignKey("foreshadows.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    chapter_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    note: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)


class PromptTemplateOverride(TimestampMixin, Base):
    """用户对某个提示词模板 key 的覆盖：对该用户的全部项目生效；停用后运行时用系统默认，内容保留。

    base_*：保存时系统默认的指纹与全文（系统默认升级后据此判断 outdated、展示三方差异）。
    """

    __tablename__ = "prompt_overrides"
    __table_args__ = (UniqueConstraint("user_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    system: Mapped[str] = mapped_column(Text, default="", nullable=False)
    user: Mapped[str] = mapped_column(Text, default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    base_fingerprint: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    base_system: Mapped[str] = mapped_column(Text, default="", nullable=False)
    base_user: Mapped[str] = mapped_column(Text, default="", nullable=False)


class PromptOverrideVersion(TimestampMixin, Base):
    """覆盖的历史版本（每个覆盖保留最近 MAX_VERSIONS 个）。"""

    __tablename__ = "prompt_override_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    override_id: Mapped[int] = mapped_column(
        ForeignKey("prompt_overrides.id", ondelete="CASCADE"), index=True, nullable=False
    )
    system: Mapped[str] = mapped_column(Text, default="", nullable=False)
    user: Mapped[str] = mapped_column(Text, default="", nullable=False)
    note: Mapped[str] = mapped_column(String(200), default="", nullable=False)


class WritingStyle(TimestampMixin, Base):
    """写作风格。user_id 为空 = 内置预设（preset_key 唯一，只读，所有用户可见）。"""

    __tablename__ = "writing_styles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    preset_key: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    pov: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    rhythm: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    dialogue: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    description_density: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    tone: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    diction: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    taboos: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    sample: Mapped[str] = mapped_column(Text, default="", nullable=False)
    extra: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


@event.listens_for(WritingStyle.__table__, "after_create")
def _seed_preset_styles(target, connection, **_kw) -> None:
    """create_all（测试）建表后写入预设；生产环境由迁移 0013 写入、启动时 sync_presets 同步。"""
    connection.execute(
        target.insert(),
        [{**p, "user_id": None, "sort": i} for i, p in enumerate(PRESET_STYLES, start=1)],
    )


INSPIRATION_STATUSES = ("active", "converted")


class InspirationSession(TimestampMixin, Base):
    """灵感会话：固定步骤的立项流程，整体状态存在 state（JSON，结构由 schemas.inspiration.SessionState 校验）。

    project_id：从已有项目发起时的项目；converted_project_id：转为新项目后的项目。两者都 SET NULL。
    """

    __tablename__ = "inspiration_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True, nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active", nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}", nullable=False)
    converted_project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True, nullable=True
    )


# 知识库与剧情记忆（迁移 0015）
KB_DOCUMENT_STATUSES = ("pending", "parsing", "indexing", "ready", "ready_keyword_only", "failed")
RETRIEVAL_SOURCES = ("kb", "memory")
MEMORY_KINDS = ("summary", "plot_point", "hook", "character", "foreshadow", "manual")
MEMORY_ORIGINS = ("auto", "manual")


class KbSettings(TimestampMixin, Base):
    """项目的检索设置（每个项目一行，首次修改时创建；读取时没有行即用默认值）。记忆的召回参数也放在这里。"""

    __tablename__ = "kb_settings"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    auto_inject: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    top_k: Mapped[int] = mapped_column(Integer, default=6, server_default="6", nullable=False)
    min_score: Mapped[float] = mapped_column(Float, default=0.35, server_default="0.35", nullable=False)
    max_chars: Mapped[int] = mapped_column(Integer, default=3000, server_default="3000", nullable=False)
    memory_top_k: Mapped[int] = mapped_column(Integer, default=8, server_default="8", nullable=False)
    memory_max_chars: Mapped[int] = mapped_column(Integer, default=1500, server_default="1500", nullable=False)
    memory_min_score: Mapped[float] = mapped_column(Float, default=0.35, server_default="0.35", nullable=False)


class KbDocument(TimestampMixin, Base):
    """知识库文档：只保存解析出的全文（text），不保存原始文件；重建索引时从 text 重新分块。"""

    __tablename__ = "kb_documents"
    __table_args__ = (UniqueConstraint("project_id", "sha256"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime: Mapped[str] = mapped_column(String(100), default="", server_default="", nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # 原始文件的 sha256（同项目内去重）
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # pending / parsing / indexing / ready / ready_keyword_only / failed
    status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # 解析 / 建索引的提示（如全文超长被截断、只能关键词检索）
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]", nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # 参与注入：关闭后该文档的检索单元 enabled=false，不再被召回
    inject: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)


class KbChunk(TimestampMixin, Base):
    """知识库分块（seq 从 1 起）。heading_path 如「第一卷 > 门派 > 天剑宗」。"""

    __tablename__ = "kb_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("kb_documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str] = mapped_column(String(500), default="", server_default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class StoryMemory(TimestampMixin, Base):
    """剧情记忆。auto = 从章节分析规则抽取（重新分析时替换，按 content_key 继承钉住 / 屏蔽）；manual = 手动添加。

    章节删除时其自动记忆先被删除（memory_service.on_chapters_deleted）；手动记忆保留，chapter_id 由外键置空、章号不变。
    """

    __tablename__ = "story_memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    chapter_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), index=True, nullable=True
    )
    chapter_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # summary / plot_point / hook / character / foreshadow / manual
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 相关角色名
    characters: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]", nullable=False)
    importance: Mapped[int] = mapped_column(Integer, default=3, server_default="3", nullable=False)
    # auto / manual
    origin: Mapped[str] = mapped_column(String(8), default="auto", server_default="auto", nullable=False)
    analysis_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapter_analyses.id", ondelete="SET NULL"), index=True, nullable=True
    )
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    muted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    # sha1(kind + 规范化内容) 前 16 位：重新分析时据此继承钉住 / 屏蔽标记
    content_key: Mapped[str] = mapped_column(String(16), default="", server_default="", nullable=False)


class RetrievalChunk(TimestampMixin, Base):
    """统一的检索单元：知识库分块（source=kb，ref_id=kb_chunks.id）与剧情记忆（source=memory，ref_id=story_memories.id）。

    只保存检索所需的冗余字段，由业务写入点经 retrieval_service.upsert / delete 维护。
    embedding 为归一化后的 float32 字节串；embedding_key = provider:model:dim。version 每次更新 +1（缓存失效标记）。
    """

    __tablename__ = "retrieval_chunks"
    __table_args__ = (
        UniqueConstraint("source", "ref_id"),
        Index("ix_retrieval_chunks_project_source_enabled", "project_id", "source", "enabled"),
        # id 不复用（SQLite 默认会复用被删除的最大 rowid）：检索缓存以 max(id) 判断是否有新行
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    ref_id: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 二字组 / 英文数字词的词频 {词: 次数}（BM25）
    tokens: Mapped[dict[str, int]] = mapped_column(JSON, default=dict, nullable=False)
    length: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedding_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 仅记忆有：所属章节号（参与章节号平移）
    chapter_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
