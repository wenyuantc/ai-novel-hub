"""任务类型注册表：kind → TaskHandler。后续子任务注册新类型只需实现 TaskHandler 协议（on_success 可选）。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.providers import ProviderFactory
from app.core.config import Settings
from app.core.errors import BadRequestError
from app.core.security import SecretCipher
from app.models import Task

Event = tuple[str, dict[str, Any]]


@dataclass
class TaskContext:
    """引擎与创建请求共享的依赖。settings 每次使用时读取（测试会在运行中修改）。"""

    sessionmaker: async_sessionmaker[AsyncSession]
    settings: Settings
    cipher: SecretCipher
    factory: ProviderFactory


@dataclass
class TaskSpec:
    """validate_and_claim 的产出：待插入任务的字段。input 必须可 JSON 序列化，且含原始请求 request。"""

    project_id: int | None
    chapter_id: int | None
    resource_key: str | None
    title: str
    input: dict[str, Any] = field(default_factory=dict)
    # 同键任务排队串行（resource_key 冲突即拒绝；serial_key 冲突则等待前一个结束）
    serial_key: str | None = None


class TaskHandler(Protocol):
    kinds: tuple[str, ...]

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any]
    ) -> TaskSpec:
        """创建任务的请求内同步执行：鉴权、配置解析、SSRF / 参数校验、资源抢占。不 commit；业务错误抛 AppError。"""
        ...

    async def build_job(self, ctx: TaskContext, task: Task) -> Any:
        """任务开始运行时组装执行对象；抛 AppError 时任务失败（detail 即错误信息）。"""
        ...

    def run(self, job: Any, ctx: TaskContext) -> AsyncIterator[Event]:
        """产出 delta / continue / done / error 事件；被取消时自行在 finally 中回退。"""
        ...

    def progress(self, task: Task, text: str) -> int:
        """按已输出文本估算进度（0–95）。"""
        ...

    async def on_abort(self, ctx: TaskContext, task: Task) -> None:
        """任务未成功结束（失败 / 取消 / 中断 / 排队中取消）时清理资源；须幂等，自身吞掉异常。"""
        ...

    # 可选：async def on_success(self, ctx: TaskContext, task: Task) -> None
    #   任务写入 succeeded 后、广播 done 之前调用（引擎用 getattr 探测）；异常只记日志，不影响任务状态。


_HANDLERS: dict[str, TaskHandler] = {}


def register(handler: TaskHandler) -> TaskHandler:
    for kind in handler.kinds:
        _HANDLERS[kind] = handler
    return handler


def get_handler(kind: str) -> TaskHandler:
    handler = _HANDLERS.get(kind)
    if handler is None:
        raise BadRequestError(f"不支持的任务类型：{kind}")
    return handler
