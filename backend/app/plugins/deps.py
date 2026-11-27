from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.deps import DbDep
from app.plugins.registry import PluginRegistry, use_registry
from app.plugins.state import require_enabled


def require_plugin(plugin_id: str):
    async def dependency(db: DbDep) -> None:
        await require_enabled(db, plugin_id)
    return dependency


class PluginScope:
    def __init__(self, app: ASGIApp, registry: PluginRegistry):
        self.app = app
        self.registry = registry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        admin = "/api/v1/admin/plugins"
        protected = scope["type"] == "http" and (path == admin or path.startswith(admin + "/"))

        async def no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        with use_registry(self.registry):
            await self.app(scope, receive, no_store if protected else send)
