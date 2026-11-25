"""为预算分配配置身份，并按已入账费用建立各周期的初始值。"""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("升级时需连接数据库建立预算基线")
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.exec_driver_sql("UPDATE alembic_version SET version_num=version_num")
        # SQLite 日期函数会把 23:59:59.999999 四舍五入到次月；账单时间以 UTC 保存。
        month = "strftime('%Y-%m', substr(c.started_at, 1, 19), '+8 hours')"
    else:
        db.exec_driver_sql("LOCK TABLE ai_usage_calls IN SHARE MODE")
        month = "to_char(c.started_at AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM')"
    op.add_column("ai_usage_budgets", sa.Column("instance_key", sa.String(36), nullable=False, server_default=""))
    for budget_id in db.execute(sa.text("SELECT id FROM ai_usage_budgets")).scalars().all():
        db.execute(sa.text("UPDATE ai_usage_budgets SET instance_key=:key WHERE id=:id"),
                   {"key": str(uuid4()), "id": budget_id})

    periods = op.create_table(
        "ai_budget_periods",
        sa.Column("budget_id", sa.Integer(), sa.ForeignKey("ai_usage_budgets.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("period", sa.String(7), primary_key=True),
        sa.Column("spent_nano", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("spent_nano >= 0", name="spent"),
    )
    # 拆整币和零头避免 SQLite 累加 int64 溢出；只保存预算封顶值。
    for period, joins in [
        (month, "JOIN ai_usage_calls c ON c.payer_id=b.user_id"),
        ("'all'", "JOIN projects p ON p.id=b.project_id JOIN ai_usage_calls c "
         "ON c.project_id=p.id AND c.payer_id=p.user_id AND c.author_paid"),
    ]:
        rows = db.execute(sa.text(f"""
            SELECT b.id, b.limit_nano, {period} AS period,
                   SUM(c.amount_nano / 1000000000) AS whole,
                   SUM(c.amount_nano % 1000000000) AS remainder
            FROM ai_usage_budgets b {joins}
            WHERE c.currency=b.currency AND c.amount_nano IS NOT NULL
            GROUP BY b.id, b.limit_nano, {period if period == month else 'b.project_id'}
        """)).mappings()
        for batch in rows.partitions(500):
            db.execute(periods.insert(), [
                {"budget_id": row["id"], "period": row["period"],
                 "spent_nano": min(int(row["whole"]) * 10**9 + int(row["remainder"]), row["limit_nano"])}
                for row in batch
            ])


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("需要连接数据库检查预算通知数据")
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.exec_driver_sql("UPDATE alembic_version SET version_num=version_num")
    else:
        db.exec_driver_sql("LOCK TABLE ai_budget_periods, notifications IN ACCESS EXCLUSIVE MODE")
    if (db.exec_driver_sql("SELECT 1 FROM ai_budget_periods LIMIT 1").first()
            or db.exec_driver_sql("SELECT 1 FROM notifications WHERE kind='budget' LIMIT 1").first()):
        raise RuntimeError("存在预算通知或周期进度，请先备份，拒绝降级")
    op.drop_table("ai_budget_periods")
    op.drop_column("ai_usage_budgets", "instance_key")
