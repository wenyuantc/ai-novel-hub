"""knowledge base

知识库与剧情记忆：新建 kb_settings（每项目一行，project_id 为主键）、kb_documents（project_id + sha256 唯一）、
kb_chunks（document_id CASCADE）、story_memories（chapter_id / analysis_id SET NULL）、retrieval_chunks
（统一检索单元，(source, ref_id) 唯一，(project_id, source, enabled) 索引）。只建表、不回填；
ai_configs.model_type 本来就是字符串，新增的 embedding 取值不需要改表。downgrade 删除这五张表。

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-28 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: Union[str, Sequence[str], None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "kb_settings",
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("auto_inject", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("top_k", sa.Integer(), server_default="6", nullable=False),
        sa.Column("min_score", sa.Float(), server_default="0.35", nullable=False),
        sa.Column("max_chars", sa.Integer(), server_default="3000", nullable=False),
        sa.Column("memory_top_k", sa.Integer(), server_default="8", nullable=False),
        sa.Column("memory_max_chars", sa.Integer(), server_default="1500", nullable=False),
        sa.Column("memory_min_score", sa.Float(), server_default="0.35", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_kb_settings_project_id_projects"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("project_id", name=op.f("pk_kb_settings")),
    )

    op.create_table(
        "kb_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime", sa.String(length=100), server_default="", nullable=False),
        sa.Column("size", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column("warnings", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.Column("char_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("inject", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_kb_documents_project_id_projects"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_documents")),
        sa.UniqueConstraint("project_id", "sha256", name=op.f("uq_kb_documents_project_id")),
    )
    with op.batch_alter_table("kb_documents", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_kb_documents_project_id"), ["project_id"], unique=False)

    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.String(length=500), server_default="", nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), server_default="0", nullable=False),
        sa.Column("end_offset", sa.Integer(), server_default="0", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["kb_documents.id"],
            name=op.f("fk_kb_chunks_document_id_kb_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_chunks")),
    )
    with op.batch_alter_table("kb_chunks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_kb_chunks_document_id"), ["document_id"], unique=False)

    op.create_table(
        "story_memories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("chapter_id", sa.Integer(), nullable=True),
        sa.Column("chapter_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("characters", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("importance", sa.Integer(), server_default="3", nullable=False),
        sa.Column("origin", sa.String(length=8), server_default="auto", nullable=False),
        sa.Column("analysis_id", sa.Integer(), nullable=True),
        sa.Column("pinned", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("muted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("content_key", sa.String(length=16), server_default="", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_story_memories_project_id_projects"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["chapter_id"], ["chapters.id"], name=op.f("fk_story_memories_chapter_id_chapters"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["chapter_analyses.id"],
            name=op.f("fk_story_memories_analysis_id_chapter_analyses"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_story_memories")),
    )
    with op.batch_alter_table("story_memories", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_story_memories_project_id"), ["project_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_story_memories_chapter_id"), ["chapter_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_story_memories_analysis_id"), ["analysis_id"], unique=False)

    op.create_table(
        "retrieval_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("ref_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tokens", sa.JSON(), nullable=False),
        sa.Column("length", sa.Integer(), server_default="0", nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("embedding_key", sa.String(length=200), nullable=True),
        sa.Column("chapter_no", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("pinned", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_retrieval_chunks_project_id_projects"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retrieval_chunks")),
        sa.UniqueConstraint("source", "ref_id", name=op.f("uq_retrieval_chunks_source")),
        # id 不复用（检索缓存以 max(id) 判断是否有新行）；PostgreSQL 忽略该参数
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("retrieval_chunks", schema=None) as batch_op:
        batch_op.create_index(
            "ix_retrieval_chunks_project_source_enabled", ["project_id", "source", "enabled"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("retrieval_chunks", schema=None) as batch_op:
        batch_op.drop_index("ix_retrieval_chunks_project_source_enabled")
    op.drop_table("retrieval_chunks")
    with op.batch_alter_table("story_memories", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_story_memories_analysis_id"))
        batch_op.drop_index(batch_op.f("ix_story_memories_chapter_id"))
        batch_op.drop_index(batch_op.f("ix_story_memories_project_id"))
    op.drop_table("story_memories")
    with op.batch_alter_table("kb_chunks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_kb_chunks_document_id"))
    op.drop_table("kb_chunks")
    with op.batch_alter_table("kb_documents", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_kb_documents_project_id"))
    op.drop_table("kb_documents")
    op.drop_table("kb_settings")
