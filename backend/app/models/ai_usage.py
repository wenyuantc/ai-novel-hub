from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    event,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import DDL

from app.models.base import Base

NANOS_PER_UNIT = 1_000_000_000
MAX_RATE_NANO = 1_000_000 * NANOS_PER_UNIT
MAX_BUDGET_NANO = 1_000_000_000 * NANOS_PER_UNIT
MAX_TOKENS = 1_000_000_000
MAX_IMAGES = 1_000
MAX_CALL_AMOUNT_NANO = 2 * MAX_BUDGET_NANO
PENDING_CALL_WHERE = "status = 'pending'"


class AiUsageTracking(Base):
    __tablename__ = "ai_usage_tracking"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


event.listen(AiUsageTracking.__table__, "after_create", DDL("INSERT INTO ai_usage_tracking (id) VALUES (1)"))


class AiModelPrice(Base):
    __tablename__ = "ai_model_prices"
    __table_args__ = (
        CheckConstraint("currency IN ('CNY', 'USD')", name="currency"),
        CheckConstraint(
            "(model_type = 'text' AND image_price_nano IS NULL "
            "AND (input_price_nano IS NOT NULL OR output_price_nano IS NOT NULL)) OR "
            "(model_type = 'embedding' AND input_price_nano IS NOT NULL "
            "AND output_price_nano IS NULL AND image_price_nano IS NULL) OR "
            "(model_type = 'image' AND image_price_nano IS NOT NULL "
            "AND input_price_nano IS NULL AND output_price_nano IS NULL)",
            name="rate_shape",
        ),
        *(
            CheckConstraint(f"{column} BETWEEN 0 AND {MAX_RATE_NANO}", name=column)
            for column in ("input_price_nano", "output_price_nano", "image_price_nano")
        ),
    )

    config_instance_key: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_configs.instance_key", ondelete="CASCADE"), primary_key=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    model_type: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    input_price_nano: Mapped[int | None] = mapped_column(BigInteger)
    cached_input_price_nano: Mapped[int | None] = mapped_column(BigInteger, CheckConstraint(
        "cached_input_price_nano IS NULL OR "
        f"(model_type = 'text' AND cached_input_price_nano BETWEEN 0 AND {MAX_RATE_NANO})",
        name="cached_input_price_nano",
    ))
    output_price_nano: Mapped[int | None] = mapped_column(BigInteger)
    image_price_nano: Mapped[int | None] = mapped_column(BigInteger)
    off_peak: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AiUsageBudget(Base):
    __tablename__ = "ai_usage_budgets"
    __table_args__ = (
        CheckConstraint("(user_id IS NULL) != (project_id IS NULL)", name="scope"),
        CheckConstraint("currency IN ('CNY', 'USD')", name="currency"),
        CheckConstraint(f"limit_nano BETWEEN 1 AND {MAX_BUDGET_NANO}", name="limit"),
        CheckConstraint("threshold_percent BETWEEN 1 AND 100", name="threshold"),
        UniqueConstraint("user_id", "currency", name="uq_ai_usage_budgets_user_currency"),
        UniqueConstraint("project_id", "currency", name="uq_ai_usage_budgets_project_currency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    limit_nano: Mapped[int] = mapped_column(BigInteger, nullable=False)
    threshold_percent: Mapped[int] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AiUsageCall(Base):
    __tablename__ = "ai_usage_calls"
    __table_args__ = (
        CheckConstraint("length(id) = 36", name="id_length"),
        CheckConstraint("project_id IS NULL OR had_project", name="project_origin"),
        CheckConstraint("model_type IN ('text', 'embedding', 'image')", name="model_type"),
        CheckConstraint("currency IN ('CNY', 'USD')", name="currency"),
        CheckConstraint(
            "(status = 'pending' AND finished_at IS NULL AND pricing_status = 'pending' AND sent IS NOT FALSE) OR "
            "(status = 'not_sent' AND finished_at IS NOT NULL AND pricing_status = 'not_sent' AND sent IS FALSE) OR "
            "(status IN ('succeeded', 'failed', 'cancelled', 'interrupted') AND finished_at IS NOT NULL AND "
            "((sent IS NULL AND pricing_status = 'unconfirmed') OR "
            "(sent IS TRUE AND pricing_status IN ('priced', 'missing_price', 'missing_usage'))))",
            name="result_shape",
        ),
        CheckConstraint("(pricing_status = 'priced') = (amount_nano IS NOT NULL)", name="priced_amount"),
        CheckConstraint(f"amount_nano BETWEEN 0 AND {MAX_CALL_AMOUNT_NANO}", name="amount_range"),
        *(
            CheckConstraint(
                f"({source} = 'unknown' AND {quantity} IS NULL) OR "
                f"({source} IN ('provider', 'estimated') AND {quantity} IS NOT NULL "
                f"AND {quantity} BETWEEN 0 AND {maximum})",
                name=source,
            )
            for quantity, source, maximum in (
                ("input_tokens", "input_source", MAX_TOKENS),
                ("output_tokens", "output_source", MAX_TOKENS),
                ("image_count", "image_source", MAX_IMAGES),
            )
        ),
        Index("ix_ai_usage_calls_started_id", "started_at", "id"),
        Index("ix_ai_usage_calls_actor_started_id", "actor_id", "started_at", "id"),
        Index("ix_ai_usage_calls_payer_started_id", "payer_id", "started_at", "id"),
        Index("ix_ai_usage_calls_project_started_id", "project_id", "started_at", "id"),
        Index("ix_ai_usage_calls_chapter_id", "chapter_id"),
        Index("ix_ai_usage_calls_task_id", "task_id"),
        Index(
            "ix_ai_usage_calls_pending_started_at", "started_at",
            sqlite_where=text(PENDING_CALL_WHERE), postgresql_where=text(PENDING_CALL_WHERE),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    payer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    had_project: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id", ondelete="SET NULL"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    task_key: Mapped[str | None] = mapped_column(String(36))
    author_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    config_instance_key: Mapped[str | None] = mapped_column(String(36))
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    model_type: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    sent: Mapped[bool | None] = mapped_column(Boolean)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cached_input_tokens: Mapped[int | None] = mapped_column(BigInteger, CheckConstraint(
        "cached_input_tokens IS NULL OR "
        "(model_type = 'text' AND input_tokens IS NOT NULL AND cached_input_tokens BETWEEN 0 AND input_tokens)",
        name="cached_input_tokens",
    ))
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    image_count: Mapped[int | None] = mapped_column(BigInteger)
    input_source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="unknown")
    output_source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="unknown")
    image_source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="unknown")
    currency: Mapped[str | None] = mapped_column(String(3))
    price_input_nano: Mapped[int | None] = mapped_column(BigInteger)
    price_cached_input_nano: Mapped[int | None] = mapped_column(BigInteger, CheckConstraint(
        "price_cached_input_nano IS NULL OR "
        f"(model_type = 'text' AND price_cached_input_nano BETWEEN 0 AND {MAX_RATE_NANO})",
        name="price_cached_input_nano",
    ))
    price_output_nano: Mapped[int | None] = mapped_column(BigInteger)
    price_image_nano: Mapped[int | None] = mapped_column(BigInteger)
    price_period: Mapped[str | None] = mapped_column(
        String(16), CheckConstraint("price_period IN ('fixed', 'peak', 'off_peak')", name="price_period"),
    )
    pending_price_schedule: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    amount_nano: Mapped[int | None] = mapped_column(BigInteger)
    pricing_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
