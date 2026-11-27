from app.plugins.manifest import PluginManifest
from app.plugins.registry import PluginRegistry

REGISTERED: tuple[PluginManifest, ...] = ()


def load(extra: tuple[PluginManifest, ...] = ()) -> PluginRegistry:
    return PluginRegistry((*REGISTERED, *extra))
