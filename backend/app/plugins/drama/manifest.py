"""短剧文本工作台的内置清单。"""

from app.plugins.drama.api import assets, episodes, tasks
from app.plugins.drama.handlers import DramaHandler
from app.plugins.drama.transfer import TRANSFER_RULES, backup_state, validate_backup
from app.plugins.manifest import PluginManifest

MANIFEST = PluginManifest(
    id="drama",
    name="短剧",
    description="将小说章节改编成短剧剧本，管理角色造型、场景、道具和分镜。",
    version="1.0.0",
    routers=(episodes.router, assets.router, tasks.router),
    task_handlers=(DramaHandler(),),
    transfer_rules=TRANSFER_RULES,
    transfer_validator=validate_backup,
    transfer_snapshot=backup_state,
)
