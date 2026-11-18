"""保存文本模型的缓存命中输入单价和调用用量。"""

import sqlalchemy as sa
from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None

_MAX_RATE_NANO = 1_000_000_000_000_000


def upgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    op.execute(
        "ALTER TABLE ai_model_prices ADD COLUMN cached_input_price_nano BIGINT "
        "CONSTRAINT ck_ai_model_prices_cached_input_price_nano CHECK (cached_input_price_nano IS NULL OR "
        f"(model_type = 'text' AND cached_input_price_nano BETWEEN 0 AND {_MAX_RATE_NANO}))"
    )
    op.execute(
        "ALTER TABLE ai_usage_calls ADD COLUMN cached_input_tokens BIGINT "
        "CONSTRAINT ck_ai_usage_calls_cached_input_tokens CHECK (cached_input_tokens IS NULL OR "
        "(model_type = 'text' AND input_tokens IS NOT NULL AND cached_input_tokens BETWEEN 0 AND input_tokens))"
    )
    op.execute(
        "ALTER TABLE ai_usage_calls ADD COLUMN price_cached_input_nano BIGINT "
        "CONSTRAINT ck_ai_usage_calls_price_cached_input_nano CHECK (price_cached_input_nano IS NULL OR "
        f"(model_type = 'text' AND price_cached_input_nano BETWEEN 0 AND {_MAX_RATE_NANO}))"
    )


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
        rule_rate = "json_extract(off_peak, '$.cached_input_price_nano')"
        pending_rates = (
            "json_extract(pending_price_schedule, '$.cached_input_price_nano')",
            "json_extract(pending_price_schedule, '$.off_peak.cached_input_price_nano')",
        )
        rule_windows = "json_type(off_peak, '$.additional_windows')"
        pending_windows = "json_type(pending_price_schedule, '$.off_peak.additional_windows')"
        remove_rule_rate = "json_remove(off_peak, '$.cached_input_price_nano')"
        remove_pending_rates = (
            "json_remove(pending_price_schedule, '$.cached_input_price_nano', '$.off_peak.cached_input_price_nano')"
        )
    elif db.dialect.name == "postgresql":
        op.execute("LOCK TABLE ai_model_prices, ai_usage_calls IN ACCESS EXCLUSIVE MODE")
        rule_rate = "off_peak->>'cached_input_price_nano'"
        pending_rates = (
            "pending_price_schedule->>'cached_input_price_nano'",
            "pending_price_schedule->'off_peak'->>'cached_input_price_nano'",
        )
        rule_windows = "off_peak->'additional_windows'"
        pending_windows = "pending_price_schedule->'off_peak'->'additional_windows'"
        remove_rule_rate = "CAST((CAST(off_peak AS jsonb) - 'cached_input_price_nano') AS json)"
        remove_pending_rates = (
            "CAST((CAST(pending_price_schedule AS jsonb) - 'cached_input_price_nano' "
            "#- '{off_peak,cached_input_price_nano}') AS json)"
        )
    else:
        raise RuntimeError("缓存命中计价降级不支持当前数据库")

    has_price = db.execute(sa.text(
        f"SELECT 1 FROM ai_model_prices WHERE cached_input_price_nano IS NOT NULL OR {rule_rate} IS NOT NULL LIMIT 1"
    )).first()
    has_usage = db.execute(sa.text(
        "SELECT 1 FROM ai_usage_calls WHERE cached_input_tokens IS NOT NULL OR price_cached_input_nano IS NOT NULL "
        f"OR {pending_rates[0]} IS NOT NULL OR {pending_rates[1]} IS NOT NULL LIMIT 1"
    )).first()
    if has_price is not None or has_usage is not None:
        raise RuntimeError("仍有缓存命中定价或用量记录，请先备份并确认处置方式")

    # 0062 无法读取 additional_windows，存在该键时保留完整时段规则。
    has_windows = db.execute(sa.text(
        f"SELECT 1 FROM ai_model_prices WHERE {rule_windows} IS NOT NULL UNION ALL "
        f"SELECT 1 FROM ai_usage_calls WHERE {pending_windows} IS NOT NULL LIMIT 1"
    )).first()
    if has_windows is not None:
        raise RuntimeError("仍有多个空闲时段的定价或待发送快照，请先备份并确认处置方式")

    # 0062 的价格数据类无法读取新键，即使值为 JSON null。
    op.execute(f"UPDATE ai_model_prices SET off_peak = {remove_rule_rate} WHERE off_peak IS NOT NULL")
    op.execute(
        f"UPDATE ai_usage_calls SET pending_price_schedule = {remove_pending_rates} "
        "WHERE pending_price_schedule IS NOT NULL"
    )
    op.drop_column("ai_usage_calls", "price_cached_input_nano")
    op.drop_column("ai_usage_calls", "cached_input_tokens")
    op.drop_column("ai_model_prices", "cached_input_price_nano")
