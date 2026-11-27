from app.plugins.manifest import PluginManifest
from app.plugins.registry import PluginRegistry


def load(extra: tuple[PluginManifest, ...] = ()) -> PluginRegistry:
    # 路由依赖鉴权与任务引擎，待核心模块加载完成后再装配内置清单。
    from app.plugins.builtins import REGISTERED

    return PluginRegistry((*REGISTERED, *extra))
