"""为 Agent 提案原位建立不可依赖整数主键的 UUID 身份。

Revision ID: 0047
Revises: 0046
"""

from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def _uuid_check(column: str, *, version4: bool = False) -> str:
    hexadecimal = f"replace({column}, '-', '')"
    remainder = hexadecimal
    for char in "0123456789abcdef":
        remainder = f"replace({remainder}, '{char}', '')"
    version_check = (
        f"AND substr({column}, 15, 1) = '4' AND substr({column}, 20, 1) IN ('8', '9', 'a', 'b') "
        if version4 else ""
    )
    return (
        f"CAST({column} AS TEXT) = {column} AND length({column}) = 36 "
        f"AND substr({column}, 9, 1) = '-' AND substr({column}, 14, 1) = '-' "
        f"AND substr({column}, 19, 1) = '-' AND substr({column}, 24, 1) = '-' "
        f"{version_check}"
        f"AND length({hexadecimal}) = 32 AND {remainder} = ''"
    )


def _backfill(db) -> None:
    """按主键分页，逐行分配独立 UUID，不使用由旧整数推导的伪身份。"""
    last_id = None
    while True:
        statement = "SELECT id FROM agent_changes"
        params = {}
        if last_id is not None:
            statement += " WHERE id > :last_id"
            params["last_id"] = last_id
        statement += " ORDER BY id LIMIT 500"
        rows = db.execute(sa.text(statement), params).scalars().all()
        if not rows:
            return
        db.execute(sa.text("UPDATE agent_changes SET change_key=:key WHERE id=:id"), [
            {"id": row_id, "key": str(uuid4())} for row_id in rows
        ])
        last_id = rows[-1]


def upgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        # sqlite3 legacy 模式下 DDL 不隐式 BEGIN；先取得真实事务，避免中断留下半套 DDL。
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
        # SQLite 有存量时新增 NOT NULL 列需要常量默认；空串只用于短暂回填，不允许新写入。
        # 不重建表：扩展可能有指向 agent_changes 的真实外键及级联子记录。
        op.execute(
            "ALTER TABLE agent_changes ADD COLUMN change_key VARCHAR(36) NOT NULL DEFAULT '' "
            f"CONSTRAINT ck_agent_changes_change_key_uuid4 "
            f"CHECK (change_key = '' OR ({_uuid_check('change_key', version4=True)}))"
        )
        _backfill(db)
        op.create_index("ux_agent_changes_change_key", "agent_changes", ["change_key"], unique=True)
        for action in ("INSERT", "UPDATE OF change_key"):
            name = "insert" if action == "INSERT" else "update"
            op.execute(
                f"CREATE TRIGGER agent_changes_change_key_nonempty_{name} BEFORE {action} ON agent_changes "
                "WHEN NEW.change_key = '' BEGIN SELECT RAISE(ABORT, 'agent change identity required'); END"
            )
    else:
        op.add_column("agent_changes", sa.Column("change_key", sa.String(36), nullable=True))
        _backfill(db)
        op.alter_column("agent_changes", "change_key", existing_type=sa.String(36), nullable=False, server_default="")
        op.create_check_constraint(
            op.f("ck_agent_changes_change_key_uuid4"), "agent_changes", _uuid_check("change_key", version4=True),
        )
        op.create_index("ux_agent_changes_change_key", "agent_changes", ["change_key"], unique=True)
    # 旧任务整数 FK 即使当前有 UUID，也无法证明它就是原来创建消息/提案的任务：不回填。
    for table in ("agent_messages", "agent_changes"):
        check = f"turn_task_key IS NULL OR ({_uuid_check('turn_task_key')})"
        if db.dialect.name == "sqlite":
            op.execute(f"ALTER TABLE {table} ADD COLUMN turn_task_key VARCHAR(36) "
                       f"CONSTRAINT ck_{table}_turn_task_key_uuid CHECK ({check})")
        else:
            op.add_column(table, sa.Column("turn_task_key", sa.String(36), nullable=True))
            op.create_check_constraint(op.f(f"ck_{table}_turn_task_key_uuid"), table, check)


def downgrade() -> None:
    db = op.get_bind()
    if (db.execute(sa.text("SELECT 1 FROM agent_changes LIMIT 1")).first() is not None
            or db.execute(sa.text("SELECT 1 FROM agent_messages WHERE turn_task_key IS NOT NULL LIMIT 1"))
            .first() is not None):
        raise RuntimeError("仍有智能体提案身份，拒绝有损降级；请先备份并安全清空提案")
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    for table in ("agent_changes", "agent_messages"):
        if db.dialect.name != "sqlite":
            op.drop_constraint(op.f(f"ck_{table}_turn_task_key_uuid"), table, type_="check")
        op.drop_column(table, "turn_task_key")
    if db.dialect.name == "sqlite":
        op.execute("DROP TRIGGER agent_changes_change_key_nonempty_insert")
        op.execute("DROP TRIGGER agent_changes_change_key_nonempty_update")
    else:
        op.drop_constraint(op.f("ck_agent_changes_change_key_uuid4"), "agent_changes", type_="check")
    op.drop_index("ux_agent_changes_change_key", table_name="agent_changes")
    op.drop_column("agent_changes", "change_key")
