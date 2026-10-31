"""项目工具契约。业务服务的提交降为 flush，由提案服务持有最终事务。"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, BadRequestError, NotFoundError
from app.models import Base, Project
from app.services.task_engine.registry import TaskContext

ToolKind = Literal["read", "small", "delete", "task", "text", "external"]
Preview = dict[str, Any]


@dataclass
class ToolCtx:
    db: AsyncSession
    project: Project
    user_id: int
    task_context: TaskContext

    @property
    def project_id(self) -> int:
        # SAVEPOINT 回滚会过期 ORM 属性；主键从 identity 读取，不触发隐式异步 IO。
        return inspect(self.project).identity[0]


class TransactionSession:
    """复用现有 service，而不允许它提交或回滚调用方的审批事务。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    def __getattr__(self, name: str) -> Any:
        return getattr(self.db, name)

    async def commit(self) -> None:
        await self.db.flush()

    async def rollback(self) -> None:
        # service 在捕获 IntegrityError 后会立即重新抛出业务异常，外层 SAVEPOINT 负责回滚。
        pass


async def lock_project(db: AsyncSession, project_id: int, user_id: int) -> None:
    """串行项目写入，并在 SQLite legacy 模式中确保 SAVEPOINT 外已有物理事务。

    显式保留 updated_at，防止 SQLAlchemy onupdate 将无值变化的行锁变成项目编辑。
    """
    from app.services.project_service import get_owned_project

    await get_owned_project(db, user_id, project_id, lock=True)


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IdArgs(EmptyArgs):
    id: int = Field(gt=0)


def arguments(name: str, schema: type[BaseModel], *, with_id: bool = False) -> type[BaseModel]:
    fields = {"id": (int, Field(gt=0))} if with_id else {}
    return create_model(name, __base__=schema, __config__=ConfigDict(extra="forbid"), **fields)


def serialized(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [serialized(item) for item in value]
    if isinstance(value, dict):
        return {key: serialized(item) for key, item in value.items()}
    if isinstance(value, Base):
        return jsonable_encoder({col.key: getattr(value, col.key) for col in inspect(value).mapper.columns})
    return jsonable_encoder(value)


def error_result(exc: AppError | ValidationError) -> dict:
    if isinstance(exc, NotFoundError):
        return {"error": "不存在"}
    if isinstance(exc, ValidationError):
        errors = [
            {"field": ".".join(str(v) for v in e["loc"]), "message": e["msg"]}
            for e in exc.errors(include_input=False, include_context=False, include_url=False)
        ]
        return {"error": "工具参数校验失败", "errors": errors}
    return {"error": exc.detail}


Operation = Callable[[ToolCtx, BaseModel], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    schema: type[BaseModel]
    kind: ToolKind
    operation: Operation
    preview_operation: Operation | None = None
    id_model: type | None = None

    @property
    def parameters(self) -> dict:
        return self.schema.model_json_schema()

    async def _validate(self, ctx: ToolCtx, args: dict) -> BaseModel:
        from .references import validate_references

        # 在丢弃未知字段之前检查引用，嵌套 params 中的越权 ID 也不例外。
        await validate_references(ctx, args, id_model=self.id_model)
        return self.schema.model_validate(args)

    async def run(self, ctx: ToolCtx, args: dict) -> dict:
        try:
            if self.kind != "read":
                raise BadRequestError("写操作需要先生成提案")
            result = serialized(await self.operation(ctx, await self._validate(ctx, args)))
            return result if isinstance(result, dict) else {"items": result}
        except (AppError, ValidationError) as exc:
            return error_result(exc)

    async def preview(self, ctx: ToolCtx, args: dict) -> Preview:
        from app.services.agent import effects

        try:
            data = await self._validate(ctx, args)
            if self.kind == "read":
                raise BadRequestError("读工具没有修改预览")
            await lock_project(ctx.db, ctx.project_id, ctx.user_id)
            tx = await ctx.db.begin_nested()
            try:
                scoped = replace(ctx, db=TransactionSession(ctx.db))
                if self.preview_operation is not None:
                    result = await self.preview_operation(scoped, data)
                else:
                    before = await effects.snapshot(ctx.db, ctx.project_id)
                    await self.operation(scoped, data)
                    await ctx.db.flush()
                    after = await effects.snapshot(ctx.db, ctx.project_id)
                    changes = effects.diff(before, after)
                    targets = [
                        change
                        for change in changes
                        if self.id_model is None or change["table"] == self.id_model.__tablename__
                    ]
                    row = (targets[0]["before"] or targets[0]["after"]) if targets else {}
                    result = {
                        "title": self.description,
                        "target": row.get("name") or row.get("title") or self.description,
                        "diff": preview_diff(changes, include_insert_sort="sort" in args),
                        "warnings": ["删除会同时影响下列关联数据"] if self.kind == "delete" else [],
                        # 比较完整旧行，未直接改写的字段及隐藏的正文指纹变化也会刷新提案。
                        "guards": [
                            {
                                "table": change["table"],
                                "pk": change["pk"],
                                "hash": hashlib.sha256(
                                    json.dumps(change["before"], ensure_ascii=False, sort_keys=True).encode()
                                ).hexdigest(),
                            }
                            for change in changes
                            if change["before"] is not None
                        ],
                    }
                return serialized(result)
            finally:
                await tx.rollback()
                await ctx.db.refresh(ctx.project)
        except (AppError, ValidationError) as exc:
            return error_result(exc)

    async def execute(self, ctx: ToolCtx, args: dict) -> dict:
        try:
            data = await self._validate(ctx, args)
            if self.kind == "read":
                raise BadRequestError("读工具不能执行修改")
            await lock_project(ctx.db, ctx.project_id, ctx.user_id)
            async with ctx.db.begin_nested():
                value = await self.operation(replace(ctx, db=TransactionSession(ctx.db)), data)
                await ctx.db.flush()
                result = serialized(value)
            return result if isinstance(result, dict) else {"items": result} if result is not None else {"ok": True}
        except (AppError, ValidationError) as exc:
            await ctx.db.refresh(ctx.project)
            return error_result(exc)


FIELD_LABELS = {
    "id": "编号",
    "project_id": "项目",
    "character_id": "角色",
    "organization_id": "组织",
    "career_id": "职业",
    "chapter_id": "章节",
    "outline_id": "大纲",
    "relationship_id": "人物关系",
    "type_id": "关系类型",
    "a_id": "角色甲",
    "b_id": "角色乙",
    "role_a": "甲方身份",
    "role_b": "乙方身份",
    "role_type": "角色定位",
    "aliases": "别名",
    "age": "年龄",
    "gender": "性别",
    "life_status": "生存状态",
    "always_in_context": "始终提供给模型",
    "relations_text": "关系备注",
    "genre": "主题材",
    "synopsis": "作品简介",
    "theme": "主题",
    "tags": "标签",
    "selling_points": "卖点",
    "audience": "目标读者",
    "target_words": "目标字数",
    "default_style_id": "默认写作风格",
    "org_type": "组织类型",
    "purpose": "宗旨",
    "location": "所在地",
    "power_level": "实力",
    "position": "职位或阶段顺序",
    "kind": "类型",
    "role": "主副职业",
    "requirements": "要求",
    "abilities": "能力",
    "rules": "规则",
    "intimacy": "亲密度",
    "category": "分类",
    "character_ids": "关联角色",
    "key_events": "关键事件",
    "emotion": "情感基调",
    "goal": "叙事目标",
    "est_words": "预计字数",
    "seq": "顺序",
    "importance": "重要度",
    "plant_hint": "埋设提示",
    "reveal_hint": "回收提示",
    "planned_plant_chapter": "计划埋设章节",
    "actual_plant_chapter": "实际埋设章节",
    "planned_resolve_chapter": "计划回收章节",
    "actual_resolve_chapter": "实际回收章节",
    "include_in_context": "提供给模型",
    "source": "来源",
    "changes": "关联变化",
    "ai_config_id": "模型配置",
    "instruction": "补充要求",
    "style": "写作风格",
    "skill_id": "Skill",
    "auto_continue": "自动续写",
    "analysis_id": "分析结果",
    "suggestion_ids": "选择的建议",
    "preserve": "保留要素",
    "start_chapter_id": "起始章节",
    "end_chapter_id": "结束章节",
    "include_written": "包含已写章节",
    "use_plan_words": "采用细纲字数",
    "review_every": "审核间隔",
    "skip_on_fail": "失败时跳过",
    "count": "数量",
    "direction": "发展方向",
    "new_volume": "新卷",
    "name": "名称",
    "title": "标题",
    "personality": "性格",
    "profile": "简介",
    "background": "背景",
    "appearance": "外貌",
    "current_state": "当前状态",
    "content": "内容",
    "description": "说明",
    "summary": "梗概",
    "chapter_no": "章节号",
    "volume_no": "卷号",
    "volume_title": "卷名",
    "status": "状态",
    "sort": "排序",
    "parent_id": "上级组织",
    "stage_id": "职业阶段",
    "note": "备注",
}


def preview_diff(changes: list[dict], *, include_insert_sort: bool = False) -> list[dict]:
    """临时新增 ID 用稳定占位符，PG 序列前进不会使同一提案永远无法确认。"""
    inserted: dict[tuple[str, Any], str] = {}
    counts: dict[str, int] = {}
    for change in changes:
        if change["before"] is None:
            table = change["table"]
            counts[table] = counts.get(table, 0) + 1
            if "id" in change["pk"]:
                inserted[(table, change["pk"]["id"])] = f"新增{table}:{counts[table]}"

    def normalized(table: str, row: dict | None) -> dict:
        if row is None:
            return {}
        result = dict(row)
        for column in Base.metadata.tables[table].columns:
            target = next((fk.column.table.name for fk in column.foreign_keys), table if column.primary_key else None)
            if target is not None and column.name in result:
                result[column.name] = inserted.get((target, result[column.name]), result[column.name])
        return result

    result = []
    for change in changes:
        table = change["table"]
        before, after = normalized(table, change["before"]), normalized(table, change["after"])
        pk = normalized(table, change["pk"])
        key = ":".join(str(value) for value in pk.values())
        for field in sorted(before.keys() | after.keys()):
            if field.startswith("_") or field in {"id", "project_id", "pair_low", "pair_high", "created_at"}:
                continue
            if field == "sort" and change["before"] is None and not include_insert_sort:
                continue
            if before.get(field) in (None, "", [], {}) and after.get(field) in (None, "", [], {}):
                continue
            if before.get(field) != after.get(field):
                result.append(
                    {
                        "field": f"{table}.{key}.{field}",
                        "label": FIELD_LABELS.get(field, field),
                        "before": before.get(field),
                        "after": after.get(field),
                    }
                )
    return result
