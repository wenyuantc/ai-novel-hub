"""提示词组件：作者个人的可复用提示词片段，在 Skill 正文与模板覆盖中以 `{{> 标识 }}` 引用。

用户级资源，不属于项目、不进项目备份；引用关系不建表，查询时扫描本人的 Skill 与覆盖（见 prompt_component_service）。
"""

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PromptComponent(TimestampMixin, Base):
    __tablename__ = "prompt_components"
    __table_args__ = (
        UniqueConstraint("user_id", "slug"),
        CheckConstraint("revision >= 1", name="revision_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="", server_default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # 每次修改 +1；更新时作为 CAS 条件，旧修订保存 → 409 stale_revision
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
