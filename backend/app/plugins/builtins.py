from app.plugins.drama.manifest import MANIFEST as DRAMA
from app.plugins.manifest import PluginManifest

REGISTERED: tuple[PluginManifest, ...] = (DRAMA,)
