"""版本化的第三方只读接口，与网页登录管理路由分离。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import AwareDatetime

from app.core.deps import DbDep
from app.core.open_api_deps import OpenApiUser
from app.schemas import open_api as out
from app.schemas.chapter import ChapterStatus
from app.schemas.organization import MemberStatus
from app.schemas.project import ProjectStatus
from app.schemas.relationship import RelationCategory, RelationStatus
from app.schemas.story import WorldCategory
from app.services import story_service
from app.services.export_service import ExportFormat
from app.services.open_api import catalog, entities

PageNumber = Annotated[int, Query(ge=1, description="页码，从 1 开始。")]
PageSize = Annotated[int, Query(ge=1, le=100, description="每页数量，默认 20，最大 100。")]
Search = Annotated[str | None, Query(max_length=100, description="按字面量匹配名称，不把 % 和 _ 当作通配符。")]

ERROR_RESPONSES = {
    400: {"model": out.OpenError, "description": "请求参数不满足业务约束，例如没有可导出的正文。"},
    401: {"model": out.OpenError, "description": "缺少、无效、过期或已撤销的 Key，或所属账号不可用。"},
    404: {"model": out.OpenError, "description": "资源不存在、不属于密钥所有者或小说不在授权范围内。"},
    422: {"model": out.OpenValidationError, "description": "参数校验失败，detail 为中文说明，errors 不含请求原文。"},
    429: {
        "model": out.OpenError,
        "description": "该 Key 当前分钟额度已用完。",
        "headers": {"Retry-After": {"description": "再次请求前至少等待的秒数。", "schema": {"type": "integer"}}},
    },
}


async def _private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "X-API-Key"


router = APIRouter(prefix=catalog.PREFIX, responses=ERROR_RESPONSES, dependencies=[Depends(_private_response)])


@router.get("/me", response_model=out.KeyInfo, tags=["接入"], summary="检查当前密钥与授权")
async def key_info(user: OpenApiUser) -> out.KeyInfo:
    """返回密钥名称、只读权限及授权范围，不返回完整 Key 或账户管理资料。"""
    return catalog.key_info(user)


@router.get("/novels", response_model=out.Page[out.Novel], tags=["小说"], summary="获取已授权小说列表")
async def list_novels(
    request: Request,
    user: OpenApiUser,
    db: DbDep,
    page: PageNumber = 1,
    page_size: PageSize = 20,
    q: Search = None,
    status: ProjectStatus | None = None,
) -> out.Page[out.Novel]:
    """仅返回授权小说，按小说元数据更新时间、ID 降序；支持书名及写作状态筛选。"""
    prefix = request.scope.get("root_path", "").rstrip("/") + catalog.PREFIX
    return await catalog.list_novels(db, user, page=page, page_size=page_size, q=q, status=status, url_prefix=prefix)


@router.get("/novels/{novel_id}", response_model=out.Novel, tags=["小说"], summary="获取小说信息")
async def get_novel(novel_id: int, request: Request, user: OpenApiUser, db: DbDep) -> out.Novel:
    """含简介、类型、状态、章节统计和封面地址。updated_at 只表示元数据，content_updated_at 表示现存章节行最近更新。"""
    prefix = request.scope.get("root_path", "").rstrip("/") + catalog.PREFIX
    return await catalog.get_novel(db, user, novel_id, url_prefix=prefix)


@router.get(
    "/novels/{novel_id}/chapters", response_model=out.Page[out.ChapterBrief], tags=["章节"], summary="获取分卷章节目录"
)
async def list_chapters(
    novel_id: int,
    user: OpenApiUser,
    db: DbDep,
    page: PageNumber = 1,
    page_size: PageSize = 20,
    status: ChapterStatus | None = None,
    updated_after: Annotated[
        AwareDatetime | None,
        Query(description="严格晚于此 UTC/带时区时间的章节行；不含删除事件，也不保证捕获关联大纲变更。"),
    ] = None,
) -> out.Page[out.ChapterBrief]:
    """按 (chapter_no, id) 排序，包含空章和生成中章节。目录不含正文；更新筛选不等于完整增量同步。"""
    return await catalog.list_chapters(
        db, user, novel_id, page=page, page_size=page_size, status=status, updated_after=updated_after
    )


@router.get(
    "/novels/{novel_id}/chapters/{chapter_id}", response_model=out.ChapterDetail, tags=["章节"], summary="获取单章正文"
)
async def get_chapter(novel_id: int, chapter_id: int, user: OpenApiUser, db: DbDep) -> out.ChapterDetail:
    """返回当前保存的正文和目录元信息；空章 content 为空字符串，生成中状态不表示已完成。"""
    return await catalog.get_chapter(db, user, novel_id, chapter_id)


@router.get(
    "/novels/{novel_id}/cover",
    response_class=Response,
    tags=["小说"],
    summary="下载小说封面",
    responses={
        200: {
            "description": "封面原始图片字节，实际类型取决于已保存图片。",
            "content": {
                mime: {"schema": {"type": "string", "format": "binary"}}
                for mime in ("image/png", "image/jpeg", "image/webp")
            },
        }
    },
)
async def get_cover(novel_id: int, user: OpenApiUser, db: DbDep) -> Response:
    """图片请求同样要携带 X-API-Key；无封面返回 404，不把 Key 拼入图片 URL。"""
    cover = await catalog.get_cover(db, user, novel_id)
    return Response(
        content=cover.data, media_type=cover.mime_type, headers={"Cache-Control": "no-store", "Vary": "X-API-Key"}
    )


@router.get(
    "/novels/{novel_id}/export",
    response_class=Response,
    tags=["小说"],
    summary="导出全文",
    responses={
        200: {
            "description": "全文文件，文件名由 Content-Disposition 返回。",
            "content": {
                mime: {"schema": {"type": "string", "format": "binary"}}
                for mime in ("text/plain", "text/markdown", "application/epub+zip")
            },
            "headers": {
                "Content-Disposition": {
                    "description": "下载文件名（含 UTF-8 filename*）。",
                    "schema": {"type": "string"},
                },
                "X-Export-Warnings": {
                    "description": "EPUB 跳过空章的提示，UTF-8 百分号编码。",
                    "schema": {"type": "string"},
                },
            },
        }
    },
)
async def export_novel(
    novel_id: int,
    user: OpenApiUser,
    db: DbDep,
    fmt: Annotated[ExportFormat, Query(alias="format", description="txt、md 或 epub。默认 txt。")] = "txt",
    chapter_ids: Annotated[list[str] | None, Query(description="可选章节 ID；支持逗号分隔或重复参数。")] = None,
    include_synopsis: Annotated[bool, Query(description="EPUB 是否包含简介页。")] = True,
) -> Response:
    """沿用平台导出：跳过生成中与空白正文章节；EPUB 的空章提示由 X-Export-Warnings 返回。"""
    file = await catalog.export_novel(db, user, novel_id, fmt, chapter_ids, include_synopsis=include_synopsis)
    headers = {"Content-Disposition": file.content_disposition, "Cache-Control": "no-store", "Vary": "X-API-Key"}
    if file.warnings_header:
        headers["X-Export-Warnings"] = file.warnings_header
    return Response(content=file.body, media_type=file.media_type, headers=headers)


@router.get(
    "/novels/{novel_id}/characters",
    response_model=out.Page[out.Character],
    tags=["创作设定"],
    summary="获取人物档案列表",
)
async def list_characters(
    novel_id: int,
    user: OpenApiUser,
    db: DbDep,
    page: PageNumber = 1,
    page_size: PageSize = 20,
    q: Search = None,
) -> out.Page[out.Character]:
    """按 (sort, id) 排序，可按人物姓名筛选。当前人物处境可能包含后续剧情。"""
    return await entities.list_story(
        db, user, novel_id, story_service.CHARACTERS, out.Character, page=page, page_size=page_size, q=q
    )


@router.get(
    "/novels/{novel_id}/characters/{character_id}",
    response_model=out.Character,
    tags=["创作设定"],
    summary="获取人物详情",
)
async def get_character(novel_id: int, character_id: int, user: OpenApiUser, db: DbDep) -> out.Character:
    """获取当前保存的人物档案，不返回写作注入配置。"""
    return await entities.get_story(db, user, novel_id, character_id, story_service.CHARACTERS, out.Character)


@router.get(
    "/novels/{novel_id}/world", response_model=out.Page[out.WorldSetting], tags=["创作设定"], summary="获取世界观列表"
)
async def list_world(
    novel_id: int,
    user: OpenApiUser,
    db: DbDep,
    page: PageNumber = 1,
    page_size: PageSize = 20,
    category: WorldCategory | None = None,
) -> out.Page[out.WorldSetting]:
    """按 (sort, id) 排序，可按地点、势力、物品、规则、历史或其他分类筛选。"""
    return await entities.list_story(
        db, user, novel_id, story_service.WORLD, out.WorldSetting, page=page, page_size=page_size, category=category
    )


@router.get(
    "/novels/{novel_id}/world/{setting_id}",
    response_model=out.WorldSetting,
    tags=["创作设定"],
    summary="获取世界观详情",
)
async def get_world(novel_id: int, setting_id: int, user: OpenApiUser, db: DbDep) -> out.WorldSetting:
    return await entities.get_story(db, user, novel_id, setting_id, story_service.WORLD, out.WorldSetting)


@router.get(
    "/novels/{novel_id}/outlines", response_model=out.Page[out.Outline], tags=["大纲"], summary="获取分卷大纲列表"
)
async def list_outlines(
    novel_id: int,
    user: OpenApiUser,
    db: DbDep,
    page: PageNumber = 1,
    page_size: PageSize = 20,
    volume_no: Annotated[int | None, Query(ge=1)] = None,
) -> out.Page[out.Outline]:
    """按 (volume_no, sort, chapter_no, id) 排序，分卷由 volume_no / volume_title 表示。"""
    return await entities.list_story(
        db, user, novel_id, story_service.OUTLINES, out.Outline, page=page, page_size=page_size, volume_no=volume_no
    )


@router.get(
    "/novels/{novel_id}/outlines/{outline_id}", response_model=out.Outline, tags=["大纲"], summary="获取大纲详情"
)
async def get_outline(novel_id: int, outline_id: int, user: OpenApiUser, db: DbDep) -> out.Outline:
    return await entities.get_story(db, user, novel_id, outline_id, story_service.OUTLINES, out.Outline)


@router.get(
    "/novels/{novel_id}/outlines/{outline_id}/plans",
    response_model=out.Page[out.ChapterPlan],
    tags=["大纲"],
    summary="获取大纲下的章节细纲",
)
async def list_plans(
    novel_id: int,
    outline_id: int,
    user: OpenApiUser,
    db: DbDep,
    page: PageNumber = 1,
    page_size: PageSize = 20,
) -> out.Page[out.ChapterPlan]:
    """按 (seq, id) 排序；seq 是纲内序号。未建章的 chapter_id / chapter_no 为空，人物引用已过滤删除项。"""
    return await entities.list_plans(db, user, novel_id, outline_id, page=page, page_size=page_size)


@router.get(
    "/novels/{novel_id}/organizations",
    response_model=out.Page[out.Organization],
    tags=["创作设定"],
    summary="获取组织列表",
)
async def list_organizations(
    novel_id: int,
    user: OpenApiUser,
    db: DbDep,
    page: PageNumber = 1,
    page_size: PageSize = 20,
    q: Search = None,
) -> out.Page[out.Organization]:
    """按 (sort, id) 排序。member_count 是所有状态成员总数，不是仅在职人数。"""
    return await entities.list_organizations(db, user, novel_id, page=page, page_size=page_size, q=q)


@router.get(
    "/novels/{novel_id}/organizations/{organization_id}",
    response_model=out.Organization,
    tags=["创作设定"],
    summary="获取组织详情",
)
async def get_organization(
    novel_id: int,
    organization_id: int,
    user: OpenApiUser,
    db: DbDep,
) -> out.Organization:
    return await entities.get_organization(db, user, novel_id, organization_id)


@router.get(
    "/novels/{novel_id}/organizations/{organization_id}/members",
    response_model=out.Page[out.OrganizationMember],
    tags=["创作设定"],
    summary="获取组织成员",
)
async def list_members(
    novel_id: int,
    organization_id: int,
    user: OpenApiUser,
    db: DbDep,
    page: PageNumber = 1,
    page_size: PageSize = 20,
    status: MemberStatus | None = None,
) -> out.Page[out.OrganizationMember]:
    """按成员 ID 排序，默认包含 active / left / expelled / dead，可按 status 筛选。"""
    return await entities.list_members(
        db, user, novel_id, organization_id, page=page, page_size=page_size, status=status
    )


@router.get(
    "/novels/{novel_id}/relationships",
    response_model=out.Page[out.Relationship],
    tags=["人物关系"],
    summary="获取人物关系",
)
async def list_relationships(
    novel_id: int,
    user: OpenApiUser,
    db: DbDep,
    page: PageNumber = 1,
    page_size: PageSize = 20,
    character_id: Annotated[int | None, Query(description="人物视角；必须属于该小说。")] = None,
    status: RelationStatus | None = None,
    category: RelationCategory | None = None,
) -> out.Page[out.Relationship]:
    """按关系 ID 排序；一对人物仅一条关系。指定 character_id 后返回 self_role / other_role；默认包含所有关系状态。"""
    return await entities.list_relationships(
        db, user, novel_id, page=page, page_size=page_size, character_id=character_id, status=status, category=category
    )


@router.get(
    "/novels/{novel_id}/relationships/{relationship_id}",
    response_model=out.Relationship,
    tags=["人物关系"],
    summary="获取单条人物关系",
)
async def get_relationship(
    novel_id: int,
    relationship_id: int,
    user: OpenApiUser,
    db: DbDep,
) -> out.Relationship:
    """a 是 b 的 role_a，b 是 a 的 role_b；此端点不指定人物视角。"""
    return await entities.get_relationship(db, user, novel_id, relationship_id)


@router.get(
    "/novels/{novel_id}/relationship-types",
    response_model=out.Page[out.RelationshipType],
    tags=["人物关系"],
    summary="获取可见关系类型",
)
async def list_relationship_types(
    novel_id: int,
    user: OpenApiUser,
    db: DbDep,
    page: PageNumber = 1,
    page_size: PageSize = 20,
) -> out.Page[out.RelationshipType]:
    """全局预置优先，再按 (sort, id)；只含全局与当前小说类型，builtin 表示全局预置。"""
    return await entities.list_relationship_types(db, user, novel_id, page=page, page_size=page_size)
