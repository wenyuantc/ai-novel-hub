"""开放读取接口：真实鉴权、小说隔离、领域契约与专属 OpenAPI。"""

import gc
import weakref
from datetime import UTC, datetime, timedelta
from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest
from sqlalchemy import event, update

from app.models import (
    Chapter,
    ChapterCharacter,
    ChapterPlan,
    Character,
    Organization,
    OrganizationMember,
    Outline,
    ProjectCover,
    Relationship,
    RelationshipType,
    WorldSetting,
)
from tests.helpers import create_project, register

OPEN = "/api/open/v1"
STAMP = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
COVER = b"\x89PNG\r\n\x1a\nopen-api-cover"


async def new_key(client, project_ids=(), *, all_projects=False):
    response = await client.post(
        "/api/v1/api-keys",
        json={
            "name": "外部阅读器",
            "all_projects": all_projects,
            "project_ids": list(project_ids),
            "expires_at": None,
        },
    )
    assert response.status_code == 201, response.text
    return {"X-API-Key": response.json()["key"]}


async def seed_domain(app, novel_id, *, suffix=""):
    async with app.state.sessionmaker() as db:
        hero = Character(
            project_id=novel_id,
            name=f"林远{suffix}",
            profile="主角档案",
            sort=2,
            aliases=["小远"],
            always_in_context=True,
        )
        mentor = Character(project_id=novel_id, name=f"苏师{suffix}", sort=1)
        world = WorldSetting(project_id=novel_id, title="云梦山", category="place", content="终年云雾")
        org = Organization(project_id=novel_id, name=f"剑宗{suffix}", description="组织档案")
        late = Outline(
            project_id=novel_id, volume_no=1, volume_title="初入山门", chapter_no=2, title="后排序大纲", sort=10
        )
        first = Outline(
            project_id=novel_id, volume_no=1, volume_title="初入山门", chapter_no=9, title="先排序大纲", sort=1
        )
        rel_type = RelationshipType(project_id=novel_id, role_a="师父", role_b="徒弟", category="social")
        db.add_all([hero, mentor, world, org, late, first, rel_type])
        await db.flush()
        written = Chapter(
            project_id=novel_id,
            outline_id=late.id,
            chapter_no=2,
            title="拜师",
            content="林远拜入山门。",
            word_count=7,
            status="done",
            updated_at=STAMP,
        )
        blank = Chapter(
            project_id=novel_id,
            outline_id=first.id,
            chapter_no=9,
            title="空白占位",
            content="",
            word_count=0,
            status="draft",
            updated_at=STAMP + timedelta(seconds=1),
        )
        db.add_all([written, blank])
        await db.flush()
        plan = ChapterPlan(
            project_id=novel_id,
            outline_id=late.id,
            seq=1,
            title="入门细纲",
            character_ids=[hero.id, 999999],
            chapter_id=written.id,
        )
        rel = Relationship(
            project_id=novel_id,
            a_id=mentor.id,
            b_id=hero.id,
            pair_low=min(mentor.id, hero.id),
            pair_high=max(mentor.id, hero.id),
            type_id=rel_type.id,
            intimacy=70,
            status="normal",
            description="师徒",
        )
        member = OrganizationMember(organization_id=org.id, character_id=hero.id, status="left", position="弟子")
        db.add_all([plan, rel, member, ProjectCover(project_id=novel_id, mime_type="image/png", data=COVER)])
        await db.flush()
        await db.execute(update(Chapter).where(Chapter.id == written.id).values(plan_id=plan.id, updated_at=STAMP))
        # 已建章的选角以章 cast 为权威；细纲只保留历史镜像供兼容读取验证。
        db.add(ChapterCharacter(chapter_id=written.id, character_id=hero.id))
        await db.commit()
        return {
            "hero": hero.id,
            "mentor": mentor.id,
            "world": world.id,
            "org": org.id,
            "outline": late.id,
            "first_outline": first.id,
            "written": written.id,
            "blank": blank.id,
            "plan": plan.id,
            "relation": rel.id,
            "type": rel_type.id,
        }


@pytest.fixture
async def catalog(app, client, make_client):
    await register(client)
    novel = await create_project(client, title="星海_100%", synopsis="作品简介", tags=["科幻"])
    another = await create_project(client, title="星海X100Y")
    outsider = make_client()
    await register(outsider, "bob")
    foreign = await create_project(outsider)
    first = await seed_domain(app, novel["id"])
    second = await seed_domain(app, another["id"], suffix="二")
    headers = await new_key(client, [novel["id"]])
    return {
        "novel": novel["id"],
        "another": another["id"],
        "foreign": foreign["id"],
        "data": first,
        "other_data": second,
        "headers": headers,
    }


async def test_open_api_requires_its_own_key_even_with_cookie(client, catalog):
    for suffix in ("/me", "/novels", f"/novels/{catalog['novel']}/characters"):
        response = await client.get(OPEN + suffix)
        assert response.status_code == 401
        assert isinstance(response.json()["detail"], str)
    response = await client.get(OPEN + "/me", headers={"X-API-Key": "invalid"})
    assert response.status_code == 401
    response = await client.get(OPEN + "/me", headers=catalog["headers"])
    assert response.status_code == 200, response.text
    me = response.json()
    assert me["name"] == "外部阅读器" and me["project_ids"] == [catalog["novel"]]
    assert all(k not in me for k in ("key", "key_hash", "key_enc", "password", "username", "user_id"))


async def test_open_novels_statistics_scope_literal_search_and_paging(client, catalog):
    response = await client.get(OPEN + "/novels", headers=catalog["headers"])
    assert response.status_code == 200, response.text
    page = response.json()
    assert (page["total"], page["page"], page["page_size"]) == (1, 1, 20)
    novel = page["items"][0]
    assert novel["id"] == catalog["novel"]
    assert (novel["chapter_count"], novel["total_words"]) == (2, 7)
    assert novel["synopsis"] == "作品简介" and novel["tags"] == ["科幻"]
    assert novel["content_updated_at"] == "2025-01-02T03:04:06Z"
    assert novel["cover_url"].startswith(f"{OPEN}/novels/{catalog['novel']}/cover")
    assert not {"user_id", "auto_analyze", "wizard_progress", "reading", "default_style_id"} & novel.keys()
    headers = await new_key(client, all_projects=True)
    response = await client.get(OPEN + "/novels", params={"q": "100%"}, headers=headers)
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["title"] == "星海_100%"
    response = await client.get(OPEN + "/novels", params={"q": "星海_"}, headers=headers)
    assert response.json()["total"] == 1
    response = await client.get(OPEN + "/novels", params={"page": 3, "page_size": 1}, headers=headers)
    assert response.json() == {"items": [], "total": 2, "page": 3, "page_size": 1}


async def test_open_chapters_volume_empty_body_and_utc_filter(client, catalog):
    base = f"{OPEN}/novels/{catalog['novel']}/chapters"
    headers = catalog["headers"]
    response = await client.get(base, headers=headers)
    assert response.status_code == 200, response.text
    chapters = response.json()["items"]
    assert [c["id"] for c in chapters] == [catalog["data"]["written"], catalog["data"]["blank"]]
    assert all("content" not in c and "project_id" not in c for c in chapters)
    assert chapters[0]["volume_title"] == "初入山门" and chapters[0]["volume_no"] == 1
    assert chapters[0]["novel_id"] == catalog["novel"]
    response = await client.get(base + f"/{catalog['data']['written']}", headers=headers)
    assert response.json()["content"] == "林远拜入山门。"
    response = await client.get(base + f"/{catalog['data']['blank']}", headers=headers)
    assert response.json()["content"] == "" and response.json()["status"] == "draft"
    response = await client.get(base, params={"updated_after": STAMP.isoformat()}, headers=headers)
    assert [c["id"] for c in response.json()["items"]] == [catalog["data"]["blank"]]
    response = await client.get(base, params={"updated_after": "2025-01-02T03:04:05"}, headers=headers)
    assert response.status_code == 422
    assert "detail" in response.json()
    response = await client.get(base, params={"status": "generating"}, headers=headers)
    assert response.json()["total"] == 0


async def test_open_domain_fields_order_and_derived_references(client, catalog):
    base = f"{OPEN}/novels/{catalog['novel']}"
    headers = catalog["headers"]
    data = catalog["data"]
    characters = (await client.get(base + "/characters", headers=headers)).json()
    assert [item["id"] for item in characters["items"]] == [data["mentor"], data["hero"]]
    hero = (await client.get(base + f"/characters/{data['hero']}", headers=headers)).json()
    assert hero["profile"] == "主角档案" and hero["aliases"] == ["小远"]
    assert "always_in_context" not in hero and "project_id" not in hero
    assert hero["created_at"].endswith("Z") and hero["updated_at"].endswith("Z")
    world = (await client.get(base + "/world", params={"category": "place"}, headers=headers)).json()
    assert world["total"] == 1 and world["items"][0]["content"] == "终年云雾"
    assert (await client.get(base + "/world", params={"category": "rule"}, headers=headers)).json()["total"] == 0
    outlines = (await client.get(base + "/outlines", headers=headers)).json()
    assert [o["id"] for o in outlines["items"]] == [data["first_outline"], data["outline"]]
    plans = (await client.get(base + f"/outlines/{data['outline']}/plans", headers=headers)).json()
    assert plans["items"][0]["character_ids"] == [data["hero"]]
    assert plans["items"][0]["chapter_no"] == 2 and plans["items"][0]["has_content"]
    org = (await client.get(base + f"/organizations/{data['org']}", headers=headers)).json()
    assert org["member_count"] == 1
    members = (await client.get(base + f"/organizations/{data['org']}/members", headers=headers)).json()
    assert members["items"][0]["character_name"] == "林远" and members["items"][0]["status"] == "left"
    response = await client.get(
        base + f"/organizations/{data['org']}/members", params={"status": "active"}, headers=headers
    )
    assert response.json()["total"] == 0
    for viewer, expected in ((data["mentor"], ("徒弟", "师父")), (data["hero"], ("师父", "徒弟"))):
        relations = (await client.get(base + "/relationships", params={"character_id": viewer}, headers=headers)).json()
        relation = relations["items"][0]
        assert (relation["other_role"], relation["self_role"]) == expected
        assert relation["a_name"] == "苏师" and relation["b_name"] == "林远"
    types = (await client.get(base + "/relationship-types", params={"page_size": 100}, headers=headers)).json()
    type_ids = {t["id"] for t in types["items"]}
    assert data["type"] in type_ids and catalog["other_data"]["type"] not in type_ids
    assert any(t["builtin"] and t["novel_id"] is None for t in types["items"])


def resource_paths(data):
    return [
        "",
        "/chapters",
        f"/chapters/{data['written']}",
        "/cover",
        "/export",
        "/characters",
        f"/characters/{data['hero']}",
        "/world",
        f"/world/{data['world']}",
        "/outlines",
        f"/outlines/{data['outline']}",
        f"/outlines/{data['outline']}/plans",
        "/organizations",
        f"/organizations/{data['org']}",
        f"/organizations/{data['org']}/members",
        "/relationships",
        f"/relationships/{data['relation']}",
        "/relationship-types",
    ]


async def test_every_open_resource_enforces_novel_scope_and_owner(client, catalog):
    for novel_id in (catalog["another"], catalog["foreign"], 999999):
        for path in resource_paths(catalog["data"]):
            response = await client.get(f"{OPEN}/novels/{novel_id}{path}", headers=catalog["headers"])
            assert response.status_code == 404, (novel_id, path, response.text)
            assert isinstance(response.json()["detail"], str)


async def test_nested_ids_cannot_be_mixed_between_owned_novels(client, catalog):
    headers = await new_key(client, all_projects=True)
    base = f"{OPEN}/novels/{catalog['novel']}"
    data = catalog["other_data"]
    for path in (
        f"/chapters/{data['written']}",
        f"/characters/{data['hero']}",
        f"/world/{data['world']}",
        f"/outlines/{data['outline']}",
        f"/outlines/{data['outline']}/plans",
        f"/organizations/{data['org']}",
        f"/organizations/{data['org']}/members",
        f"/relationships/{data['relation']}",
    ):
        response = await client.get(base + path, headers=headers)
        assert response.status_code == 404, (path, response.text)
    response = await client.get(base + "/relationships", params={"character_id": data["hero"]}, headers=headers)
    assert response.status_code == 404


async def test_each_domain_list_paginates_and_detail_is_available(client, catalog):
    base = f"{OPEN}/novels/{catalog['novel']}"
    data = catalog["data"]
    for path in (
        "/characters",
        "/world",
        "/outlines",
        "/organizations",
        "/relationships",
        "/relationship-types",
        f"/outlines/{data['outline']}/plans",
        f"/organizations/{data['org']}/members",
    ):
        first = await client.get(base + path, params={"page_size": 1}, headers=catalog["headers"])
        assert first.status_code == 200, (path, first.text)
        page = first.json()
        assert len(page["items"]) == 1 and page["page_size"] == 1 and page["page"] == 1
        after = await client.get(
            base + path, params={"page": page["total"] + 1, "page_size": 1}, headers=catalog["headers"]
        )
        assert after.json()["items"] == [] and after.json()["total"] == page["total"]
    for path in (f"/world/{data['world']}", f"/outlines/{data['outline']}", f"/relationships/{data['relation']}"):
        response = await client.get(base + path, headers=catalog["headers"])
        assert response.status_code == 200, response.text
        assert response.json()["novel_id"] == catalog["novel"]


@pytest.mark.parametrize("query", [{"page": 0}, {"page_size": 101}, {"page_size": 0}, {"status": "unknown"}])
async def test_open_list_validation(client, catalog, query):
    response = await client.get(OPEN + "/novels", params=query, headers=catalog["headers"])
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


async def test_open_cover_and_all_download_formats(client, catalog):
    base = f"{OPEN}/novels/{catalog['novel']}"
    response = await client.get(base + "/cover", headers=catalog["headers"])
    assert response.status_code == 200 and response.content == COVER
    assert response.headers["content-type"] == "image/png"
    for fmt, mime in (("txt", "text/plain"), ("md", "text/markdown"), ("epub", "application/epub+zip")):
        response = await client.get(base + "/export", params={"format": fmt}, headers=catalog["headers"])
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith(mime)
        assert f".{fmt}" in response.headers["content-disposition"]
        if fmt == "epub":
            assert "X-Export-Warnings" in response.headers
            with ZipFile(BytesIO(response.content)) as archive:
                assert archive.read("mimetype") == b"application/epub+zip"
        else:
            assert "林远拜入山门。" in response.text
    response = await client.get(base + "/export", params={"format": "json"}, headers=catalog["headers"])
    assert response.status_code == 422


async def test_open_export_rejects_other_novel_chapter_ids(client, catalog):
    headers = await new_key(client, all_projects=True)
    response = await client.get(
        f"{OPEN}/novels/{catalog['novel']}/export",
        params={"chapter_ids": catalog["other_data"]["written"]},
        headers=headers,
    )
    assert response.status_code == 404
    response = await client.get(
        f"{OPEN}/novels/{catalog['novel']}/export",
        params={"chapter_ids": str(catalog["data"]["written"])},
        headers=headers,
    )
    assert response.status_code == 200 and "林远拜入山门。" in response.text


async def test_open_empty_novel_and_missing_cover(client, catalog):
    novel = await create_project(client, title="尚未落笔")
    headers = await new_key(client, [novel["id"]])
    response = await client.get(f"{OPEN}/novels/{novel['id']}", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert (data["chapter_count"], data["total_words"]) == (0, 0)
    assert data["content_updated_at"] is None and data["cover_url"] is None
    assert (await client.get(f"{OPEN}/novels/{novel['id']}/cover", headers=headers)).status_code == 404
    assert (await client.get(f"{OPEN}/novels/{novel['id']}/export", headers=headers)).status_code == 400


async def test_open_generating_chapter_is_readable_but_not_exported(app, client, catalog):
    chapter_id = catalog["data"]["blank"]
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, chapter_id)
        chapter.status = "generating"
        chapter.content = "旧稿待替换"
        chapter.word_count = 5
        await db.commit()
    base = f"{OPEN}/novels/{catalog['novel']}"
    response = await client.get(base + f"/chapters/{chapter_id}", headers=catalog["headers"])
    assert response.json()["status"] == "generating" and response.json()["content"] == "旧稿待替换"
    response = await client.get(base + "/export", headers=catalog["headers"])
    assert response.status_code == 200 and "旧稿待替换" not in response.text


async def test_open_novel_queries_do_not_grow_per_novel(app, client, catalog):
    # 比较一条和多条响应所需的查询数，防止把统计 / 封面改成逐本查询。
    from app.models import Project

    async with app.state.sessionmaker() as db:
        owned = await db.get(Project, catalog["novel"])
        db.add_all(Project(user_id=owned.user_id, title=f"批量作品{i}") for i in range(12))
        await db.commit()
    headers = await new_key(client, all_projects=True)
    selects = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", record)
    try:
        one = await client.get(OPEN + "/novels", params={"page_size": 1}, headers=headers)
        first_count = len(selects)
        selects.clear()
        many = await client.get(OPEN + "/novels", params={"page_size": 20}, headers=headers)
        assert one.status_code == many.status_code == 200
        assert len(many.json()["items"]) > len(one.json()["items"])
        assert len(selects) == first_count
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", record)


@pytest.mark.parametrize(
    "settings_overrides",
    [
        {
            "cors_origins": "https://reader.example.test",
            "open_api_rate_limit_per_minute": 1,
        }
    ],
)
async def test_open_rate_limit_wait_header_is_visible_to_allowed_cors(client, catalog):
    headers = {**catalog["headers"], "Origin": "https://reader.example.test"}
    assert (await client.get(OPEN + "/me", headers=headers)).status_code == 200
    response = await client.get(OPEN + "/me", headers=headers)
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1
    assert "Retry-After" in response.headers["Access-Control-Expose-Headers"]
    assert response.headers["Access-Control-Allow-Origin"] == "https://reader.example.test"


async def test_open_only_key_cannot_access_internal_routes_or_mutate(client, catalog, make_client):
    anonymous = make_client()
    headers = catalog["headers"]
    for path in ("/api/v1/auth/me", "/api/v1/projects", "/api/v1/api-keys"):
        response = await anonymous.get(path, headers=headers)
        assert response.status_code == 401
    response = await client.post(
        f"{OPEN}/novels/{catalog['novel']}/characters", json={"name": "不应新建"}, headers=headers
    )
    assert response.status_code == 405


async def test_open_docs_schema_and_swagger_are_public_and_separate(client):
    schema_response = await client.get(OPEN + "/openapi.json")
    assert schema_response.status_code == 200
    schema = schema_response.json()
    assert len(schema["paths"]) == 20
    scheme = schema["components"]["securitySchemes"]["PlatformAPIKey"]
    assert (scheme["type"], scheme["in"], scheme["name"]) == ("apiKey", "header", "X-API-Key")
    operation_ids = []
    for path, operations in schema["paths"].items():
        assert path.startswith(OPEN + "/") and set(operations) == {"get"}
        operation = operations["get"]
        assert operation["security"] == [{"PlatformAPIKey": []}]
        assert {"200", "401", "404", "422", "429"} <= operation["responses"].keys()
        operation_ids.append(operation["operationId"])
    assert len(operation_ids) == len(set(operation_ids))
    assert all("/api-keys" not in p and "/api/v1" not in p for p in schema["paths"])
    cover = schema["paths"][OPEN + "/novels/{novel_id}/cover"]["get"]["responses"]["200"]["content"]
    assert "image/png" in cover and cover["image/png"]["schema"]["format"] == "binary"
    download = schema["paths"][OPEN + "/novels/{novel_id}/export"]["get"]["responses"]["200"]["content"]
    assert {"text/plain", "text/markdown", "application/epub+zip"} <= download.keys()
    docs = await client.get(OPEN + "/docs")
    assert docs.status_code == 200 and "swagger-ui" in docs.text
    assert OPEN + "/openapi.json" in docs.text and '"persistAuthorization": false' in docs.text
    assert (await client.get(OPEN + "/novels")).status_code == 401
    default_schema = (await client.get("/openapi.json")).json()
    assert "/api/v1/projects" in default_schema["paths"]


async def test_open_docs_do_not_retain_closed_applications(tmp_path, fake_llm):
    from app.main import create_app
    from tests.helpers import make_settings

    async def closed_application(label):
        application = create_app(
            make_settings(tmp_path / label), provider_factory=fake_llm.factory, start_task_engine=False
        )
        references = tuple(weakref.ref(value) for value in (
            application, application.state.task_engine, application.state.engine,
        ))
        application.state.open_api_schema = {
            "openapi": "3.1.0", "info": {"title": label, "version": "1"}, "paths": {},
        }
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application, root_path=f"/{label}"), base_url="http://testserver"
            ) as probe:
                schema = await probe.get(OPEN + "/openapi.json")
                assert schema.status_code == 200
                assert schema.json()["info"]["title"] == label
                assert schema.json()["servers"] == [{"url": f"/{label}"}]
                docs = await probe.get(OPEN + "/docs")
                assert docs.status_code == 200
                assert f"/{label}{OPEN}/openapi.json" in docs.text
                assert application.url_path_for("openapi_schema") == OPEN + "/openapi.json"
                assert application.url_path_for("swagger_docs") == OPEN + "/docs"
        finally:
            await application.state.system_settings.close()
            await application.state.task_engine.stop()
            await application.state.engine.dispose()
        return references

    first = await closed_application("first")
    second = await closed_application("second")
    # 全部资源和客户端已关闭，分类缓存不能通过文档闭包持有旧应用及引擎。
    gc.collect()
    assert all(reference() is None for reference in (*first, *second))


async def test_open_docs_proxy_root_path_is_used_once(app, catalog):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, root_path="/proxy"), base_url="http://testserver"
    ) as client:
        docs = await client.get(OPEN + "/docs")
        assert docs.status_code == 200
        assert "/proxy/api/open/v1/openapi.json" in docs.text
        assert "/proxy/proxy/" not in docs.text
        schema = (await client.get(OPEN + "/openapi.json")).json()
        assert schema["servers"] == [{"url": "/proxy"}]
        assert all(path.startswith(OPEN) and "/proxy" not in path for path in schema["paths"])
        novel = await client.get(f"{OPEN}/novels/{catalog['novel']}", headers=catalog["headers"])
        assert novel.json()["cover_url"] == f"/proxy{OPEN}/novels/{catalog['novel']}/cover"


async def test_open_docs_and_routes_precede_spa_fallback(app, tmp_path):
    from app.main import _mount_frontend

    (tmp_path / "index.html").write_text("<html>SPA fallback</html>")
    _mount_frontend(app, str(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        assert (await client.get(OPEN + "/openapi.json")).headers["content-type"].startswith("application/json")
        assert "swagger-ui" in (await client.get(OPEN + "/docs")).text
        assert (await client.get(OPEN + "/novels")).status_code == 401
