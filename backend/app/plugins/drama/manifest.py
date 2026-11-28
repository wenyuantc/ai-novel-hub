"""短剧文本工作台的内置清单。"""

from app.plugins.drama.api import assets, episodes, images, tasks
from app.plugins.drama.handlers import DramaHandler
from app.plugins.drama.image_handler import ImageHandler
from app.plugins.drama.transfer import TRANSFER_RULES, backup_notice, backup_state, validate_backup
from app.plugins.manifest import PluginManifest

MANIFEST = PluginManifest(
    id="drama",
    name="短剧",
    description="将小说章节改编成短剧剧本，管理角色造型、场景、道具和分镜。",
    version="1.0.0",
    routers=(episodes.router, assets.router, images.router, tasks.router),
    task_handlers=(DramaHandler(), ImageHandler()),
    transfer_rules=TRANSFER_RULES,
    not_exported={
        "drama_asset_images": "原图、缩略图和生成记录属于原部署媒体存储，单书导入后可重新生成",
        "drama_episode_model_preferences": "每个操作账号选用的模型配置仅适用于当前实例",
    },
    transfer_validator=validate_backup,
    transfer_snapshot=backup_state,
    transfer_notice=backup_notice,
)
