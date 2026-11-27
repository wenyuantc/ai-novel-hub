"""外部视频任务、按秒计价和音频参考素材。"""

import sqlalchemy as sa
from alembic import op

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None

OLD_RATE_SHAPE = (
    "(model_type = 'text' AND image_price_nano IS NULL "
    "AND (input_price_nano IS NOT NULL OR output_price_nano IS NOT NULL)) OR "
    "(model_type = 'embedding' AND input_price_nano IS NOT NULL "
    "AND output_price_nano IS NULL AND image_price_nano IS NULL) OR "
    "(model_type = 'image' AND image_price_nano IS NOT NULL "
    "AND input_price_nano IS NULL AND output_price_nano IS NULL)"
)
VIDEO_RATE_SHAPE = (
    f"({OLD_RATE_SHAPE}) AND video_price_nano IS NULL OR "
    "(model_type = 'video' AND video_price_nano IS NOT NULL AND image_price_nano IS NULL "
    "AND input_price_nano IS NULL AND output_price_nano IS NULL)"
)
MAX_RATE = 1_000_000_000_000_000


def _usage_table_batch(name, inline_columns):
    # SQLite 的反射会把 0062 / 0063 的列级 CHECK 提升为表级；后续降级原位
    # DROP COLUMN 时 SQLite 无法同时移除该列的表级检查。保留它们的原始归属。
    db = op.get_bind()
    if db.dialect.name != "sqlite":
        return op.batch_alter_table(name)
    table = sa.Table(name, sa.MetaData(), autoload_with=db)
    checks = {item.name: item for item in table.constraints if isinstance(item, sa.CheckConstraint)}
    for column in inline_columns:
        check = checks[f"ck_{name}_{column}"]
        table.constraints.remove(check)
        table.c[column].constraints.add(sa.CheckConstraint(check.sqltext, name=op.f(check.name)))
    return op.batch_alter_table(name, copy_from=table)


def _media_kind(*, audio):
    with op.batch_alter_table("media_objects") as batch:
        batch.drop_constraint(op.f("ck_media_objects_kind_valid"), type_="check")
        values = "'image','video','audio'" if audio else "'image','video'"
        batch.create_check_constraint(op.f("ck_media_objects_kind_valid"), f"kind IN ({values})")


def upgrade():
    _media_kind(audio=True)
    with _usage_table_batch("ai_model_prices", ("cached_input_price_nano",)) as batch:
        batch.add_column(sa.Column("video_price_nano", sa.BigInteger()))
        batch.drop_constraint(op.f("ck_ai_model_prices_rate_shape"), type_="check")
        batch.create_check_constraint(op.f("ck_ai_model_prices_rate_shape"), VIDEO_RATE_SHAPE)
        batch.create_check_constraint(op.f("ck_ai_model_prices_video_price_nano"),
                                      f"video_price_nano BETWEEN 0 AND {MAX_RATE}")
    with _usage_table_batch("ai_usage_calls", ("price_period", "cached_input_tokens", "price_cached_input_nano")) as batch:
        batch.add_column(sa.Column("video_seconds", sa.BigInteger()))
        batch.add_column(sa.Column("price_video_nano", sa.BigInteger()))
        batch.drop_constraint(op.f("ck_ai_usage_calls_model_type"), type_="check")
        batch.create_check_constraint(op.f("ck_ai_usage_calls_model_type"),
                                      "model_type IN ('text', 'embedding', 'image', 'video')")
        batch.create_check_constraint(op.f("ck_ai_usage_calls_video_seconds"),
                                      "video_seconds IS NULL OR (model_type = 'video' AND video_seconds BETWEEN 0 AND 1000)")
        batch.create_check_constraint(op.f("ck_ai_usage_calls_price_video_nano"),
                                      f"price_video_nano IS NULL OR (model_type = 'video' AND price_video_nano BETWEEN 0 AND {MAX_RATE})")
    op.create_table(
        "ai_external_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("payer_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="SET NULL")),
        sa.Column("config_id", sa.Integer(), nullable=False),
        sa.Column("config_instance_key", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="video"),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("external_id", sa.String(512)),
        sa.Column("state", sa.String(16), nullable=False, server_default="created"),
        sa.Column("progress", sa.Integer()),
        sa.Column("result_object_id", sa.String(36), sa.ForeignKey("media_objects.id", ondelete="SET NULL")),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column("usage_call_id", sa.String(36), sa.ForeignKey("ai_usage_calls.id", ondelete="SET NULL"), unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column("owner_ref", sa.String(255)),
        sa.CheckConstraint("kind = 'video'", name="kind"),
        sa.CheckConstraint("state IN ('created','submitting','submitted','succeeded','failed','cancelled','expired')",
                           name="state"),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="progress"),
    )
    op.create_index("ix_ai_external_jobs_task_id", "ai_external_jobs", ["task_id"])
    op.create_index("ix_ai_external_jobs_project_id", "ai_external_jobs", ["project_id"])


def downgrade():
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        db.exec_driver_sql("LOCK TABLE ai_configs, ai_model_prices, ai_usage_calls, ai_external_jobs, "
                           "media_objects, tasks IN ACCESS EXCLUSIVE MODE")
    evidence = (
        "SELECT 1 FROM ai_external_jobs",
        "SELECT 1 FROM ai_configs WHERE model_type='video'",
        "SELECT 1 FROM ai_model_prices WHERE model_type='video'",
        "SELECT 1 FROM ai_usage_calls WHERE model_type='video'",
        "SELECT 1 FROM tasks WHERE kind='ai.video_probe'",
        "SELECT 1 FROM media_objects WHERE kind='audio'",
    )
    if any(db.exec_driver_sql(f"{query} LIMIT 1").first() for query in evidence):
        raise RuntimeError("仍有视频模型、任务、用量或音频素材，请先备份并确认处置方式")
    op.drop_table("ai_external_jobs")
    with _usage_table_batch("ai_usage_calls", ("price_period", "cached_input_tokens", "price_cached_input_nano")) as batch:
        for name in ("video_seconds", "price_video_nano", "model_type"):
            batch.drop_constraint(op.f(f"ck_ai_usage_calls_{name}"), type_="check")
        batch.create_check_constraint(op.f("ck_ai_usage_calls_model_type"), "model_type IN ('text', 'embedding', 'image')")
        batch.drop_column("video_seconds")
        batch.drop_column("price_video_nano")
    with _usage_table_batch("ai_model_prices", ("cached_input_price_nano",)) as batch:
        batch.drop_constraint(op.f("ck_ai_model_prices_rate_shape"), type_="check")
        batch.drop_constraint(op.f("ck_ai_model_prices_video_price_nano"), type_="check")
        batch.create_check_constraint(op.f("ck_ai_model_prices_rate_shape"), OLD_RATE_SHAPE)
        batch.drop_column("video_price_nano")
    _media_kind(audio=False)
