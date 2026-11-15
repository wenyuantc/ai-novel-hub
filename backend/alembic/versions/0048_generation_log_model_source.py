"""原位添加生成日志的可信模型来源快照；旧记录保持未知。

Revision ID: 0048
Revises: 0047
"""

import sqlalchemy as sa

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

_MODEL_SOURCE_CHECK = (
    "(model_source IS NULL AND model_config_owner_id IS NULL) OR "
    "(model_source IS NOT NULL AND model_source IN ('actor', 'owner') "
    "AND model_config_owner_id IS NOT NULL AND model_config_owner_id >= 1 "
    "AND model_config_owner_id <= 2147483647 "
    "AND model_config_owner_id = CAST(model_config_owner_id AS INTEGER))"
)


def upgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        # sqlite3 legacy 模式下先取得真实事务；两次 ADD COLUMN 失败时可整体回滚并重试。
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    op.add_column("generation_logs", sa.Column("model_config_owner_id", sa.Integer(), nullable=True))
    if db.dialect.name == "sqlite":
        # 内联跨列约束避免重建被 chapter_versions 等引用的父表。
        op.execute(
            "ALTER TABLE generation_logs ADD COLUMN model_source VARCHAR(16) "
            f"CONSTRAINT ck_generation_logs_model_source_pair CHECK ({_MODEL_SOURCE_CHECK})"
        )
    else:
        op.add_column("generation_logs", sa.Column("model_source", sa.String(16), nullable=True))
        op.create_check_constraint(
            op.f("ck_generation_logs_model_source_pair"), "generation_logs", _MODEL_SOURCE_CHECK,
        )


def downgrade() -> None:
    db = op.get_bind()
    if db.execute(sa.text(
        "SELECT 1 FROM generation_logs WHERE model_source IS NOT NULL OR model_config_owner_id IS NOT NULL LIMIT 1"
    )).first() is not None:
        raise RuntimeError("仍有可信模型来源记录，拒绝有损降级；请先完整备份并安全处理")
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    else:
        op.drop_constraint(op.f("ck_generation_logs_model_source_pair"), "generation_logs", type_="check")
    op.drop_column("generation_logs", "model_source")
    op.drop_column("generation_logs", "model_config_owner_id")
