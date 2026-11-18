"""允许模型分时定价并在用量明细中保存命中的档位。"""

import sqlalchemy as sa
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        # 在 sqlite3 legacy 模式下让新增列和版本更新处于同一写事务。
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    op.add_column("ai_model_prices", sa.Column("off_peak", sa.JSON(none_as_null=True), nullable=True))
    op.add_column("ai_usage_calls", sa.Column("pending_price_schedule", sa.JSON(none_as_null=True), nullable=True))
    op.execute(
        "ALTER TABLE ai_usage_calls ADD COLUMN price_period VARCHAR(16) "
        "CONSTRAINT ck_ai_usage_calls_price_period CHECK (price_period IN ('fixed', 'peak', 'off_peak'))"
    )


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute("LOCK TABLE ai_model_prices, ai_usage_calls IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("分时定价降级不支持当前数据库")
    has_rule = db.execute(sa.text("SELECT 1 FROM ai_model_prices WHERE off_peak IS NOT NULL LIMIT 1")).first()
    has_usage = db.execute(sa.text(
        "SELECT 1 FROM ai_usage_calls WHERE pending_price_schedule IS NOT NULL "
        "OR price_period IN ('peak', 'off_peak') LIMIT 1"
    )).first()
    if has_rule is not None or has_usage is not None:
        raise RuntimeError("仍有分时定价或分时用量记录，请先备份并确认处置方式")
    op.drop_column("ai_usage_calls", "price_period")
    op.drop_column("ai_usage_calls", "pending_price_schedule")
    op.drop_column("ai_model_prices", "off_peak")
