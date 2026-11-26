"""受部署目录约束的个人 stdio 插件。"""

import sqlalchemy as sa
from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None

_TARGET = (
    "(transport = 'stdio' AND catalog_id IS NOT NULL AND catalog_version IS NOT NULL "
    "AND url = '' AND headers_encrypted IS NULL) OR "
    "(transport IN ('sse','streamable_http') AND catalog_id IS NULL AND catalog_version IS NULL AND url <> '')"
)


def _high_water():
    if op.get_bind().dialect.name == "sqlite":
        return op.get_bind().scalar(sa.text("SELECT seq FROM sqlite_sequence WHERE name='mcp_plugins'"))
    return None


def _restore_high_water(value):
    if value is not None:
        bind = op.get_bind()
        changed = bind.execute(
            sa.text("UPDATE sqlite_sequence SET seq=max(seq,:v) WHERE name='mcp_plugins'"), {"v": value}
        )
        if not changed.rowcount:
            bind.execute(sa.text("INSERT INTO sqlite_sequence(name,seq) VALUES ('mcp_plugins',:v)"), {"v": value})


def upgrade():
    previous = _high_water()
    with op.batch_alter_table("mcp_plugins", table_kwargs={"sqlite_autoincrement": True}) as batch:
        batch.add_column(sa.Column("catalog_id", sa.String(24)))
        batch.add_column(sa.Column("catalog_version", sa.String(80)))
        batch.create_check_constraint("transport_target", _TARGET)
        batch.create_index("uq_mcp_plugins_user_catalog", ["user_id", "catalog_id"], unique=True)
    _restore_high_water(previous)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM mcp_plugins WHERE transport='stdio'")):
        raise RuntimeError("存在已启用或保留的本机 MCP 插件，请先备份并移除后降级")
    previous = _high_water()
    with op.batch_alter_table("mcp_plugins", table_kwargs={"sqlite_autoincrement": True}) as batch:
        batch.drop_index("uq_mcp_plugins_user_catalog")
        batch.drop_constraint("transport_target", type_="check")
        batch.drop_column("catalog_version")
        batch.drop_column("catalog_id")
    _restore_high_water(previous)
