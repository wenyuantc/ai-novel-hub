"""短剧任务输入、作品内素材范围及送模材料。"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.core.errors import BadRequestError, NotFoundError
from app.models import Character, WorldSetting
from app.plugins.drama.episode_schemas import PositiveId
from app.plugins.drama.models import DramaEpisode, DramaSeries, DramaShot
from app.services.setting_ai_context import build_scope

TaskKind = Literal["drama.rewrite", "drama.extract", "drama.storyboard", "drama.prompts"]
AssetType = Literal["character", "scene", "prop"]
TargetType = Literal["look", "scene", "prop", "shot"]
ASSET_KEYS = {"character": "characters", "scene": "scenes", "prop": "props"}


class PromptTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: TargetType
    id: PositiveId


class StartDramaTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: TaskKind
    ai_config_id: PositiveId | None = None
    asset_types: list[AssetType] = Field(default_factory=list, max_length=3)
    targets: list[PromptTarget] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def check_targets(self):
        if bool(self.targets) != (self.kind == "drama.prompts"):
            raise ValueError("生成提示词需指定目标")
        if len({(item.type, item.id) for item in self.targets}) != len(self.targets):
            raise ValueError("提示词目标重复")
        if self.asset_types and (self.kind != "drama.extract" or len(set(self.asset_types)) != len(self.asset_types)):
            raise ValueError("提取类型无效")
        return self


class TaskRequest(StartDramaTask):
    project_id: PositiveId
    series_id: PositiveId
    episode_id: PositiveId | None


async def subjects(db, *, series_id=None, episode_id=None):
    episode = await db.get(DramaEpisode, episode_id) if episode_id is not None else None
    if episode_id is not None and (episode is None or series_id is not None and series_id != episode.series_id):
        raise NotFoundError("短剧集不存在")
    series = await db.get(DramaSeries, episode.series_id if episode is not None else series_id)
    if series is None:
        raise NotFoundError("短剧不存在")
    return series, episode


def revisions(series, episode):
    return {"series": series.revision, "episode": episode.revision if episode is not None else None}


def script_batches(script: str, limit: int) -> list[str]:
    """尽量在场景边界分段，过长的单场再按预算切分，每字只入模一次。"""
    result: list[str] = []
    current = ""
    for scene in re.split(r"(?m)(?=^##\s+S\d+)", script):
        if len(current) + len(scene) > limit and current:
            result.append(current)
            current = ""
        while len(scene) > limit:
            result.append(scene[:limit])
            scene = scene[limit:]
        current += scene
    if current:
        result.append(current)
    return result


async def material(db, request, series, episode):
    from app.plugins.drama.services.assets import library

    context = {"style": series.style, "aspect_ratio": series.aspect_ratio}
    kind = request.kind
    if kind == "drama.rewrite":
        if not episode.source_text.strip():
            raise BadRequestError("请先填写本集的原始内容")
        return {**context, "source_text": episode.source_text}
    if kind in ("drama.extract", "drama.storyboard") and not episode.script.strip():
        raise BadRequestError("请先填写本集的剧本")
    from app.plugins.drama.services.shots import shot_details

    selected = {(target.type, target.id) for target in request.targets}
    shot_ids = [target.id for target in request.targets if target.type == "shot"]
    shots = []
    if shot_ids:
        if episode is None:
            raise BadRequestError("视频提示词需从所属集发起")
        rows = list(
            await db.scalars(
                select(DramaShot).where(
                    DramaShot.episode_id == episode.id,
                    DramaShot.id.in_(shot_ids),
                )
            )
        )
        if len(rows) != len(shot_ids):
            raise BadRequestError("分镜不属于本集")
        shots = await shot_details(db, rows)
        direct_assets = selected - {("shot", identifier) for identifier in shot_ids}
        if direct_assets:
            local = await library(db, series.id, episode.id)
            members = {("look", look["id"]) for char in local["characters"] for look in char["looks"]}
            members.update(("prop", prop["id"]) for prop in local["props"])
            members.update(("scene", scene["id"]) for scene in local["scenes"])
            if not direct_assets <= members:
                raise BadRequestError("提示词目标不属于本集")
        for shot in shots:
            selected.update(("look", lid) for lid in shot.look_ids)
            if shot.scene_id is not None:
                selected.add(("scene", shot.scene_id))
    # 分镜允许直接引用同剧素材；只有单独生成素材提示词时才要求来自发起位置的素材库。
    inventory = await library(db, series.id, None if kind == "drama.extract" or shots else request.episode_id)
    explicit = [
        ("character", row["novel_character_id"])
        for row in inventory["characters"]
        if row["novel_character_id"] is not None and any(("look", look["id"]) in selected for look in row["looks"])
    ]
    explicit += [
        ("world", row["novel_world_setting_id"])
        for row in inventory["scenes"]
        if row["novel_world_setting_id"] is not None and ("scene", row["id"]) in selected
    ]
    scope = await build_scope(
        db,
        request.project_id,
        texts=(episode.script if episode else "",),
        kinds=("character", "world"),
        legacy_kinds=("character", "world"),
        explicit=explicit,
    )
    # 关联字段曾可能复制原作内容，出站时遵守原作当前 AI 可见性；手工页面始终保留原值。
    assets = {"characters": [], "scenes": [], "props": []}
    for category, fields in (
        ("characters", ("id", "name", "appearance", "description", "role", "looks")),
        ("scenes", ("id", "location", "time_of_day", "lighting", "description")),
        ("props", ("id", "name", "category", "description")),
    ):
        for row in inventory[category]:
            item = {key: row[key] for key in fields}
            if category == "characters":
                item["looks"] = [{k: look[k] for k in ("id", "name", "styling")} for look in item["looks"]]
            if category == "characters" and (link := row["novel_character_id"]) is not None:
                if not scope.field_visible("character", link, "name"):
                    continue
                for field, source in (("role", "role_type"), ("description", "profile"), ("appearance", "appearance")):
                    if not scope.field_visible("character", link, source):
                        item[field] = ""
            if category == "scenes" and (link := row["novel_world_setting_id"]) is not None:
                if not scope.field_visible("world", link, "title"):
                    continue
                if not scope.field_visible("world", link, "content"):
                    item["description"] = ""
            assets[category].append(item)
    if kind == "drama.extract":
        roster = []
        for entry_kind, model, name in (("character", Character, "name"), ("world", WorldSetting, "title")):
            query = select(model).where(model.project_id == request.project_id).order_by(model.id)
            if entry_kind == "world":
                query = query.where(WorldSetting.category == "place")
            for row in await db.scalars(query):
                if scope.field_visible(entry_kind, row.id, name):
                    roster.append({"type": entry_kind, "name": getattr(row, name)})
        return {
            **context,
            "script": episode.script,
            "assets": assets,
            "novel_entries": roster,
            "asset_types": request.asset_types or list(ASSET_KEYS),
        }
    if kind == "drama.storyboard":
        return {**context, "script": episode.script, "assets": assets}
    indexed = {("scene", item["id"]): item for item in assets["scenes"]}
    indexed.update({("prop", item["id"]): item for item in assets["props"]})
    for char in assets["characters"]:
        for look in char["looks"]:
            indexed["look", look["id"]] = {**look, "character": char["name"], "appearance": char["appearance"]}
    for shot in shots:
        indexed["shot", shot.id] = {
            "id": shot.id,
            "title": shot.title,
            "description": shot.description,
            "shot_size": shot.shot_size,
            "atmosphere": shot.atmosphere,
            "duration_seconds": shot.duration_seconds,
            "scene": indexed.get(("scene", shot.scene_id)),
            "looks": [indexed["look", lid] for lid in shot.look_ids if ("look", lid) in indexed],
            "props": [indexed["prop", pid] for pid in shot.prop_ids if ("prop", pid) in indexed],
        }
    if any((item.type, item.id) not in indexed for item in request.targets):
        raise BadRequestError("提示词目标不属于当前素材库或剧集")
    return {**context, "targets": [{"kind": item.type, **indexed[item.type, item.id]} for item in request.targets]}


def validate_references(request, context, draft):
    if request.kind == "drama.prompts":
        actual = [(item["kind"], item["id"]) for item in draft["prompts"]]
        if sorted(actual) != sorted((item.type, item.id) for item in request.targets):
            raise ValueError("提示词目标必须与请求清单完全一致")
    if request.kind == "drama.extract" and request.asset_types:
        for kind, key in ASSET_KEYS.items():
            if kind not in request.asset_types:
                draft[key] = []
    if request.kind == "drama.storyboard":
        assets = context["assets"]
        look_ids = {look["id"] for char in assets["characters"] for look in char["looks"]}
        prop_ids = {prop["id"] for prop in assets["props"]}
        scene_ids = {None, *(scene["id"] for scene in assets["scenes"])}
        for shot in draft["shots"]:
            if (
                shot["scene_id"] not in scene_ids
                or not look_ids.issuperset(shot["look_ids"])
                or not prop_ids.issuperset(shot["prop_ids"])
            ):
                raise ValueError("分镜只能引用本集素材中的编号")
