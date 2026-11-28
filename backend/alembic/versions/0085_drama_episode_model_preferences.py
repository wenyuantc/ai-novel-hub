"""按剧集与个人账号保存文本、图片、视频模型选择。"""

import sqlalchemy as sa
from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "drama_episode_model_preferences",
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("text_config_id", sa.Integer(), nullable=True),
        sa.Column("image_config_id", sa.Integer(), nullable=True),
        sa.Column("video_config_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["drama_episodes.id"],
            name=op.f("fk_drama_episode_model_preferences_episode_id_drama_episodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["image_config_id"],
            ["ai_configs.id"],
            name=op.f("fk_drama_episode_model_preferences_image_config_id_ai_configs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["text_config_id"],
            ["ai_configs.id"],
            name=op.f("fk_drama_episode_model_preferences_text_config_id_ai_configs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_drama_episode_model_preferences_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["video_config_id"],
            ["ai_configs.id"],
            name=op.f("fk_drama_episode_model_preferences_video_config_id_ai_configs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("episode_id", "user_id", name=op.f("pk_drama_episode_model_preferences")),
    )
    op.create_index(
        op.f("ix_drama_episode_model_preferences_image_config_id"),
        "drama_episode_model_preferences",
        ["image_config_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_drama_episode_model_preferences_text_config_id"),
        "drama_episode_model_preferences",
        ["text_config_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_drama_episode_model_preferences_user_id"), "drama_episode_model_preferences", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_drama_episode_model_preferences_video_config_id"),
        "drama_episode_model_preferences",
        ["video_config_id"],
        unique=False,
    )


def downgrade():
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        db.exec_driver_sql("LOCK TABLE drama_episode_model_preferences IN ACCESS EXCLUSIVE MODE")
    if db.exec_driver_sql(
        "SELECT 1 FROM drama_episode_model_preferences WHERE text_config_id IS NOT NULL "
        "OR image_config_id IS NOT NULL OR video_config_id IS NOT NULL LIMIT 1"
    ).first():
        raise RuntimeError("仍有短剧模型偏好，请先清除选择或备份后再降级")
    op.drop_table("drama_episode_model_preferences")
