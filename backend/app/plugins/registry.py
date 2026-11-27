"""每个应用一份只读登记表。请求与执行者在各自的异步上下文中查找同一套处理器。"""

import re
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import routing

from app.models.entities import TASK_KINDS
from app.plugins.manifest import PluginManifest
from app.services.task_engine.registry import TaskHandler

_CORE_NAMESPACES = {kind.partition(".")[0] for kind in TASK_KINDS}


def _check_router(router: routing.APIRouter, prefix: str) -> None:
    if router.prefix != prefix and not router.prefix.startswith(prefix + "/"):
        raise ValueError(f"插件路由前缀无效：{router.prefix}")
    # 新版 FastAPI 延迟合成嵌套路由，旧版在 include_router 时已展开。
    contexts = getattr(routing, "iter_route_contexts", None)
    for route in contexts(router.routes) if contexts else router.routes:
        original = getattr(route, "original_route", route)
        if not isinstance(original, routing.APIRoute):
            raise ValueError(f"插件路由仅支持带 FastAPI 依赖的 HTTP 接口：{prefix}")
        if route.path != prefix and not route.path.startswith(prefix + "/"):
            raise ValueError(f"插件接口越出登记前缀：{route.path}")


class PluginRegistry:
    def __init__(self, manifests: tuple[PluginManifest, ...]):
        self._manifests = manifests
        self._by_id = {}
        self._owners = {}
        self._handlers = {}
        for item in manifests:
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", item.id) or item.id in self._by_id:
                raise ValueError(f"插件标识无效或重复：{item.id}")
            if (not item.name.strip() or len(item.name) > 50 or len(item.description) > 300
                    or not item.version.strip() or len(item.version) > 32):
                raise ValueError(f"插件元数据无效：{item.id}")
            self._by_id[item.id] = item
            prefix = f"/plugins/{item.id}"
            for router in item.routers:
                _check_router(router, prefix)
            for handler in item.task_handlers:
                for kind in handler.kinds:
                    if (item.id in _CORE_NAMESPACES or kind in self._handlers or len(kind) > 32
                            or not re.fullmatch(rf"{item.id}\.[a-z][a-z0-9_.]*", kind)):
                        raise ValueError(f"插件任务类型无效或重复：{kind}")
                    self._owners[kind] = item.id
                    self._handlers[kind] = handler

    def all(self) -> tuple[PluginManifest, ...]:
        return self._manifests

    def get(self, plugin_id: str) -> PluginManifest | None:
        return self._by_id.get(plugin_id)

    def plugin_of_kind(self, kind: str) -> str | None:
        return self._owners.get(kind)

    def handler(self, kind: str) -> TaskHandler | None:
        return self._handlers.get(kind)


EMPTY = PluginRegistry(())
current: ContextVar[PluginRegistry] = ContextVar("feature_plugins", default=EMPTY)


@contextmanager
def use_registry(registry: PluginRegistry):
    token = current.set(registry)
    try:
        yield
    finally:
        current.reset(token)
