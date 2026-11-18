"""AI 调用用量记录、模型价格和账号 / 作品预算。"""

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

MAX_RATE_NANO = 1_000_000_000_000_000
MAX_BUDGET_NANO = 1_000_000_000_000_000_000
MAX_CALL_AMOUNT_NANO = 2_000_000_000_000_000_000
PERMISSION = "ai_usage.read"
ROLES = sa.table(
    "roles", sa.column("id", sa.Integer()), sa.column("system_key", sa.String()),
    sa.column("permissions", sa.JSON()), sa.column("revision", sa.Integer()),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
AUTHORIZATION = sa.table("authorization_state", sa.column("id", sa.Integer()), sa.column("revision", sa.Integer()))


def _update_super_admin(db, *, add: bool) -> None:
    super_admin = ROLES.c.system_key == "super_admin"
    permissions = list(db.execute(sa.select(ROLES.c.permissions).where(super_admin)).scalar_one())
    if add and PERMISSION not in permissions:
        permissions.insert(permissions.index("dashboard.read") + 1, PERMISSION)
    elif not add and PERMISSION in permissions:
        permissions.remove(PERMISSION)
    else:
        return
    db.execute(ROLES.update().where(super_admin).values(
        permissions=permissions, revision=ROLES.c.revision + 1, updated_at=sa.func.now(),
    ))
    db.execute(AUTHORIZATION.update().where(AUTHORIZATION.c.id == 1).values(revision=AUTHORIZATION.c.revision + 1))


def upgrade() -> None:
    tracking = op.create_table(
        "ai_usage_tracking",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="singleton"),
    )
    op.bulk_insert(tracking, [{"id": 1}])
    op.create_table(
        "ai_model_prices",
        sa.Column("config_instance_key", sa.String(36), sa.ForeignKey("ai_configs.instance_key", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("model_type", sa.String(16), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("input_price_nano", sa.BigInteger()),
        sa.Column("output_price_nano", sa.BigInteger()),
        sa.Column("image_price_nano", sa.BigInteger()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("currency IN ('CNY', 'USD')", name="currency"),
        sa.CheckConstraint(
            "(model_type = 'text' AND image_price_nano IS NULL "
            "AND (input_price_nano IS NOT NULL OR output_price_nano IS NOT NULL)) OR "
            "(model_type = 'embedding' AND input_price_nano IS NOT NULL "
            "AND output_price_nano IS NULL AND image_price_nano IS NULL) OR "
            "(model_type = 'image' AND image_price_nano IS NOT NULL "
            "AND input_price_nano IS NULL AND output_price_nano IS NULL)",
            name="rate_shape",
        ),
        *(sa.CheckConstraint(f"{column} BETWEEN 0 AND {MAX_RATE_NANO}", name=column)
          for column in ("input_price_nano", "output_price_nano", "image_price_nano")),
    )
    op.create_table(
        "ai_usage_budgets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE")),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("limit_nano", sa.BigInteger(), nullable=False),
        sa.Column("threshold_percent", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("(user_id IS NULL) != (project_id IS NULL)", name="scope"),
        sa.CheckConstraint("currency IN ('CNY', 'USD')", name="currency"),
        sa.CheckConstraint(f"limit_nano BETWEEN 1 AND {MAX_BUDGET_NANO}", name="limit"),
        sa.CheckConstraint("threshold_percent BETWEEN 1 AND 100", name="threshold"),
        sa.UniqueConstraint("user_id", "currency", name="uq_ai_usage_budgets_user_currency"),
        sa.UniqueConstraint("project_id", "currency", name="uq_ai_usage_budgets_project_currency"),
    )
    op.create_table(
        "ai_usage_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("payer_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="SET NULL")),
        sa.Column("had_project", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id", ondelete="SET NULL")),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
        sa.Column("task_key", sa.String(36)),
        sa.Column("author_paid", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("config_instance_key", sa.String(36)),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("model_type", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("sent", sa.Boolean()),
        sa.Column("input_tokens", sa.BigInteger()),
        sa.Column("output_tokens", sa.BigInteger()),
        sa.Column("image_count", sa.BigInteger()),
        sa.Column("input_source", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("output_source", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("image_source", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("currency", sa.String(3)),
        sa.Column("price_input_nano", sa.BigInteger()),
        sa.Column("price_output_nano", sa.BigInteger()),
        sa.Column("price_image_nano", sa.BigInteger()),
        sa.Column("amount_nano", sa.BigInteger()),
        sa.Column("pricing_status", sa.String(16), nullable=False, server_default="pending"),
        sa.CheckConstraint("length(id) = 36", name="id_length"),
        sa.CheckConstraint("project_id IS NULL OR had_project", name="project_origin"),
        sa.CheckConstraint("model_type IN ('text', 'embedding', 'image')", name="model_type"),
        sa.CheckConstraint("currency IN ('CNY', 'USD')", name="currency"),
        sa.CheckConstraint(
            "(status = 'pending' AND finished_at IS NULL AND pricing_status = 'pending' AND sent IS NOT FALSE) OR "
            "(status = 'not_sent' AND finished_at IS NOT NULL AND pricing_status = 'not_sent' AND sent IS FALSE) OR "
            "(status IN ('succeeded', 'failed', 'cancelled', 'interrupted') AND finished_at IS NOT NULL AND "
            "((sent IS NULL AND pricing_status = 'unconfirmed') OR "
            "(sent IS TRUE AND pricing_status IN ('priced', 'missing_price', 'missing_usage'))))",
            name="result_shape",
        ),
        sa.CheckConstraint("(pricing_status = 'priced') = (amount_nano IS NOT NULL)", name="priced_amount"),
        sa.CheckConstraint(f"amount_nano BETWEEN 0 AND {MAX_CALL_AMOUNT_NANO}", name="amount_range"),
        *(sa.CheckConstraint(
            f"({source} = 'unknown' AND {quantity} IS NULL) OR "
            f"({source} IN ('provider', 'estimated') AND {quantity} IS NOT NULL "
            f"AND {quantity} BETWEEN 0 AND {maximum})",
            name=source,
        ) for quantity, source, maximum in (
            ("input_tokens", "input_source", 1_000_000_000),
            ("output_tokens", "output_source", 1_000_000_000),
            ("image_count", "image_source", 1_000),
        )),
    )
    for name, columns in (
        ("started_id", ["started_at", "id"]),
        ("actor_started_id", ["actor_id", "started_at", "id"]),
        ("payer_started_id", ["payer_id", "started_at", "id"]),
        ("project_started_id", ["project_id", "started_at", "id"]),
        ("chapter_id", ["chapter_id"]),
        ("task_id", ["task_id"]),
    ):
        op.create_index(f"ix_ai_usage_calls_{name}", "ai_usage_calls", columns)
    pending = sa.text("status = 'pending'")
    op.create_index(
        "ix_ai_usage_calls_pending_started_at", "ai_usage_calls", ["started_at"],
        sqlite_where=pending, postgresql_where=pending,
    )
    _update_super_admin(op.get_bind(), add=True)


def downgrade() -> None:
    db = op.get_bind()
    tables = ("ai_usage_calls", "ai_usage_budgets", "ai_model_prices")
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute(f"LOCK TABLE authorization_state, roles, {', '.join(tables)} IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("AI 用量降级不支持当前数据库")
    for table in tables:
        if db.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("仍有 AI 用量、价格或预算数据，请先备份并确认处置方式")
    other_roles = sa.or_(ROLES.c.system_key.is_(None), ROLES.c.system_key != "super_admin")
    if any(PERMISSION in value for value in db.scalars(sa.select(ROLES.c.permissions).where(other_roles))):
        raise RuntimeError("仍有角色被授予 AI 用量查看权限，请先确认并移除授权")
    _update_super_admin(db, add=False)
    for table in (*tables, "ai_usage_tracking"):
        op.drop_table(table)
