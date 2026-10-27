"""book imports

拆书导入：新建 book_imports（user_id CASCADE，expires_at 索引）；generation_logs.kind 由 String(16) 放宽到 String(32)
（拆书反推的生成记录类型 book_import.synthesize 等超过 16 字符）。chapter_versions.source 新增取值 import、
tasks.kind 新增取值 book.import，都是字符串列、没有 CHECK 约束，不改表。
downgrade 删除 book_imports；删掉拆书的生成记录与任务（旧代码不认识这些取值）后把 kind 收回 String(16)，
导入版本（source = import）改记为 baseline。

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-29 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: Union[str, Sequence[str], None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "book_imports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("size", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sha256", sa.String(length=64), server_default="", nullable=False),
        sa.Column("book_title", sa.String(length=200), server_default="", nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("split_mode", sa.String(length=8), server_default="auto", nullable=False),
        sa.Column("split_pattern", sa.String(length=100), nullable=True),
        sa.Column("chapters", sa.JSON(), nullable=False),
        sa.Column("notices", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("doc_spans", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_book_imports_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_book_imports")),
    )
    with op.batch_alter_table("book_imports", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_book_imports_user_id"), ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_book_imports_expires_at"), ["expires_at"], unique=False)

    with op.batch_alter_table("generation_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "kind", existing_type=sa.String(length=16), type_=sa.String(length=32), existing_nullable=False
        )


def downgrade() -> None:
    # 旧代码不认识拆书的生成记录类型（列也放不下）与任务类型，先删掉；导入版本改记为 baseline（内容不变）
    op.execute("DELETE FROM generation_logs WHERE kind LIKE 'book_import.%'")
    op.execute("DELETE FROM tasks WHERE kind = 'book.import'")
    op.execute("UPDATE chapter_versions SET source = 'baseline' WHERE source = 'import'")
    with op.batch_alter_table("generation_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "kind", existing_type=sa.String(length=32), type_=sa.String(length=16), existing_nullable=False
        )
    with op.batch_alter_table("book_imports", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_book_imports_expires_at"))
        batch_op.drop_index(batch_op.f("ix_book_imports_user_id"))
    op.drop_table("book_imports")
