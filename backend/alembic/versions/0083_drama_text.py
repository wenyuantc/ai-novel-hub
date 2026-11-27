"""短剧剧本、资产与分镜。"""

import sqlalchemy as sa
from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "drama_series",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("style", sa.String(length=48), server_default="3d", nullable=False),
        sa.Column("aspect_ratio", sa.String(length=8), server_default="16:9", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("aspect_ratio IN ('16:9','9:16','1:1')", name=op.f("ck_drama_series_aspect_ratio")),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_drama_series_project_id_projects"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drama_series")),
        sqlite_autoincrement=True,
    )
    op.create_index(op.f("ix_drama_series_project_id"), "drama_series", ["project_id"], unique=False)
    op.create_table(
        "drama_characters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("role", sa.Text(), server_default="", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("appearance", sa.Text(), server_default="", nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        sa.Column("novel_character_id", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["novel_character_id"],
            ["characters.id"],
            name=op.f("fk_drama_characters_novel_character_id_characters"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["drama_series.id"],
            name=op.f("fk_drama_characters_series_id_drama_series"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drama_characters")),
        sa.UniqueConstraint("series_id", "name", name=op.f("uq_drama_characters_series_id")),
        sqlite_autoincrement=True,
    )
    op.create_index(
        op.f("ix_drama_characters_novel_character_id"), "drama_characters", ["novel_character_id"], unique=False
    )
    op.create_table(
        "drama_episodes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("source_text", sa.Text(), server_default="", nullable=False),
        sa.Column("script", sa.Text(), server_default="", nullable=False),
        sa.Column("script_skipped", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("resolution", sa.String(length=8), server_default="720p", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("resolution IN ('480p','720p','1080p')", name=op.f("ck_drama_episodes_resolution")),
        sa.CheckConstraint("status IN ('draft','active','completed')", name=op.f("ck_drama_episodes_status")),
        sa.CheckConstraint("number > 0", name=op.f("ck_drama_episodes_number")),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["drama_series.id"],
            name=op.f("fk_drama_episodes_series_id_drama_series"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drama_episodes")),
        sa.UniqueConstraint("series_id", "number", name=op.f("uq_drama_episodes_series_id")),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "drama_props",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.Text(), server_default="", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("image_prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["series_id"], ["drama_series.id"], name=op.f("fk_drama_props_series_id_drama_series"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drama_props")),
        sa.UniqueConstraint("series_id", "name", name=op.f("uq_drama_props_series_id")),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "drama_scenes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("location", sa.String(length=200), nullable=False),
        sa.Column("time_of_day", sa.String(length=200), server_default="", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("lighting", sa.Text(), server_default="", nullable=False),
        sa.Column("image_prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("novel_world_setting_id", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["novel_world_setting_id"],
            ["world_settings.id"],
            name=op.f("fk_drama_scenes_novel_world_setting_id_world_settings"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["series_id"], ["drama_series.id"], name=op.f("fk_drama_scenes_series_id_drama_series"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drama_scenes")),
        sa.UniqueConstraint("series_id", "location", "time_of_day", name=op.f("uq_drama_scenes_series_id")),
        sqlite_autoincrement=True,
    )
    op.create_index(
        op.f("ix_drama_scenes_novel_world_setting_id"), "drama_scenes", ["novel_world_setting_id"], unique=False
    )
    op.create_table(
        "drama_episode_props",
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("prop_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["drama_episodes.id"],
            name=op.f("fk_drama_episode_props_episode_id_drama_episodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["prop_id"], ["drama_props.id"], name=op.f("fk_drama_episode_props_prop_id_drama_props"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("episode_id", "prop_id", name=op.f("pk_drama_episode_props")),
    )
    op.create_index(op.f("ix_drama_episode_props_prop_id"), "drama_episode_props", ["prop_id"], unique=False)
    op.create_table(
        "drama_episode_scenes",
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("scene_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["drama_episodes.id"],
            name=op.f("fk_drama_episode_scenes_episode_id_drama_episodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scene_id"],
            ["drama_scenes.id"],
            name=op.f("fk_drama_episode_scenes_scene_id_drama_scenes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("episode_id", "scene_id", name=op.f("pk_drama_episode_scenes")),
    )
    op.create_index(op.f("ix_drama_episode_scenes_scene_id"), "drama_episode_scenes", ["scene_id"], unique=False)
    op.create_table(
        "drama_episode_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("chapter_id", sa.Integer(), nullable=True),
        sa.Column("chapter_title", sa.String(length=200), nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["chapter_id"],
            ["chapters.id"],
            name=op.f("fk_drama_episode_sources_chapter_id_chapters"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["drama_episodes.id"],
            name=op.f("fk_drama_episode_sources_episode_id_drama_episodes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drama_episode_sources")),
        sa.UniqueConstraint("episode_id", "chapter_id", name="uq_drama_episode_sources_chapter"),
        sa.UniqueConstraint("episode_id", "sort", name=op.f("uq_drama_episode_sources_episode_id")),
        sqlite_autoincrement=True,
    )
    op.create_index(op.f("ix_drama_episode_sources_chapter_id"), "drama_episode_sources", ["chapter_id"], unique=False)
    op.create_table(
        "drama_looks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("styling", sa.Text(), server_default="", nullable=False),
        sa.Column("image_prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["drama_characters.id"],
            name=op.f("fk_drama_looks_character_id_drama_characters"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drama_looks")),
        sa.UniqueConstraint("character_id", "name", name=op.f("uq_drama_looks_character_id")),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "uq_drama_looks_default",
        "drama_looks",
        ["character_id"],
        unique=True,
        sqlite_where=sa.text("is_default"),
        postgresql_where=sa.text("is_default"),
    )
    op.create_table(
        "drama_shots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("shot_size", sa.Text(), server_default="", nullable=False),
        sa.Column("atmosphere", sa.Text(), server_default="", nullable=False),
        sa.Column("duration_seconds", sa.Integer(), server_default="5", nullable=False),
        sa.Column("scene_id", sa.Integer(), nullable=True),
        sa.Column("video_prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("duration_seconds BETWEEN 2 AND 30", name=op.f("ck_drama_shots_duration")),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["drama_episodes.id"],
            name=op.f("fk_drama_shots_episode_id_drama_episodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scene_id"], ["drama_scenes.id"], name=op.f("fk_drama_shots_scene_id_drama_scenes"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drama_shots")),
        sa.UniqueConstraint("episode_id", "position", name=op.f("uq_drama_shots_episode_id")),
        sqlite_autoincrement=True,
    )
    op.create_index(op.f("ix_drama_shots_scene_id"), "drama_shots", ["scene_id"], unique=False)
    op.create_table(
        "drama_episode_looks",
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("look_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["drama_episodes.id"],
            name=op.f("fk_drama_episode_looks_episode_id_drama_episodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["look_id"], ["drama_looks.id"], name=op.f("fk_drama_episode_looks_look_id_drama_looks"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("episode_id", "look_id", name=op.f("pk_drama_episode_looks")),
    )
    op.create_index(op.f("ix_drama_episode_looks_look_id"), "drama_episode_looks", ["look_id"], unique=False)
    op.create_table(
        "drama_shot_looks",
        sa.Column("shot_id", sa.Integer(), nullable=False),
        sa.Column("look_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["look_id"], ["drama_looks.id"], name=op.f("fk_drama_shot_looks_look_id_drama_looks"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["shot_id"], ["drama_shots.id"], name=op.f("fk_drama_shot_looks_shot_id_drama_shots"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("shot_id", "look_id", name=op.f("pk_drama_shot_looks")),
    )
    op.create_index(op.f("ix_drama_shot_looks_look_id"), "drama_shot_looks", ["look_id"], unique=False)
    op.create_table(
        "drama_shot_props",
        sa.Column("shot_id", sa.Integer(), nullable=False),
        sa.Column("prop_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["prop_id"], ["drama_props.id"], name=op.f("fk_drama_shot_props_prop_id_drama_props"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["shot_id"], ["drama_shots.id"], name=op.f("fk_drama_shot_props_shot_id_drama_shots"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("shot_id", "prop_id", name=op.f("pk_drama_shot_props")),
    )
    op.create_index(op.f("ix_drama_shot_props_prop_id"), "drama_shot_props", ["prop_id"], unique=False)


def downgrade():
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        db.exec_driver_sql("LOCK TABLE drama_series IN ACCESS EXCLUSIVE MODE")
    if db.exec_driver_sql("SELECT 1 FROM drama_series LIMIT 1").first():
        raise RuntimeError("仍有短剧，请先备份并确认处置方式")
    op.drop_table("drama_shot_props")
    op.drop_table("drama_shot_looks")
    op.drop_table("drama_episode_looks")
    op.drop_table("drama_shots")
    op.drop_table("drama_looks")
    op.drop_table("drama_episode_sources")
    op.drop_table("drama_episode_scenes")
    op.drop_table("drama_episode_props")
    op.drop_table("drama_scenes")
    op.drop_table("drama_props")
    op.drop_table("drama_episodes")
    op.drop_table("drama_characters")
    op.drop_table("drama_series")
