"""仅供测试的内置插件，有独立业务数据、受登录保护的接口和一个纯文本任务。"""

from fastapi import APIRouter
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select

from app.core.deps import CurrentUser, DbDep
from app.plugins import PluginManifest
from app.services.task_engine.registry import TaskSpec

router = APIRouter(prefix="/plugins/example")
records = Table("sample_plugin_records", MetaData(), Column("id", Integer, primary_key=True),
                Column("user_id", Integer, nullable=False), Column("value", String, nullable=False))


@router.get("/records")
async def read_records(user: CurrentUser, db: DbDep):
    return list(await db.scalars(select(records.c.value).where(records.c.user_id == user.id).order_by(records.c.id)))


@router.post("/records")
async def add_record(value: str, user: CurrentUser, db: DbDep):
    await db.execute(insert(records).values(user_id=user.id, value=value))
    await db.commit()
    return {"value": value}


class Handler:
    kinds = ("example.render",)
    execution_identity_aware = False

    async def validate_and_claim(self, db, ctx, *, user_id, kind, params):
        return TaskSpec(None, None, None, "示例任务", {"request": params})

    async def build_job(self, ctx, task):
        return task

    async def run(self, job, ctx):
        yield "done", {"text": "完成"}

    def progress(self, task, text):
        return 50

    async def on_abort(self, ctx, task):
        pass


SAMPLE = PluginManifest("example", "示例", "测试插件", "1", (router,), (Handler(),))
