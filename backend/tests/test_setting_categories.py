"""分类／整理标签 HTTP 写边界、目录依赖、只读快照与恢复图校验。"""

import asyncio

import pytest
from sqlalchemy import event, func, select, update

from app.core.errors import AppError, ConflictError, UnauthorizedError
from app.models import (
    AdminAuditLog,
    Character,
    Project,
    SettingCategory,
    SettingCategoryRule,
    SettingTextField,
    User,
)
from app.models.setting_fields import SettingFieldDefinition, SettingFieldOption
from app.services import setting_category_query_service as query
from app.services.authorization_service import AuthenticatedActor
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role
from tests.test_setting_category_query import snapshot_database as snapshot_database

KINDS = ("character", "world", "organization", "career")


async def _setup(client):
    user = await register(client)
    project = await create_project(client)
    return user, project["id"], f"/api/v1/projects/{project['id']}"


async def _entry(client, base, kind, name="条目"):
    endpoint = {"character": "characters", "world": "world", "organization": "organizations", "career": "careers"}[kind]
    body = {"title": name, "category": "place"} if kind == "world" else {"name": name}
    if kind == "career":
        body["stages"] = [{"name": "第一阶"}]
    response = await client.post(f"{base}/{endpoint}", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _spec(**extra):
    return {
        "name": "同行",
        "applicable_types": ["character"],
        "match_mode": "all",
        "rules": [{"kind": "tag", "tag": "同盟"}],
        **extra,
    }


async def _versions(client, base):
    response = await client.get(f"{base}/setting-categories")
    assert response.status_code == 200, response.text
    data = response.json()
    return {
        "expected_categories_revision": data["categories_revision"],
        "expected_fields_revision": data["fields_revision"],
    }


async def _create(client, base, **extra):
    response = await client.post(f"{base}/setting-categories", json={**await _versions(client, base), **_spec(**extra)})
    assert response.status_code == 201, response.text
    return response.json()


async def _dropdown(client, base, *, label="阵营", types=None):
    revision = (await client.get(f"{base}/setting-field-definitions")).json()["fields_revision"]
    response = await client.post(
        f"{base}/setting-field-definitions",
        json={
            "expected_fields_revision": revision,
            "label": label,
            "field_type": "dropdown",
            "applicable_types": types or ["character"],
        },
    )
    assert response.status_code == 201, response.text
    definition = response.json()["item"]
    response = await client.post(
        f"{base}/setting-field-definitions/{definition['id']}/options",
        json={
            "expected_fields_revision": response.json()["fields_revision"],
            "label": "同盟",
        },
    )
    assert response.status_code == 201, response.text
    return definition["id"], response.json()["item"]["id"]


@pytest.mark.parametrize("kind", KINDS)
async def test_tags_normalize_clear_and_do_not_leak_into_old_entity_dtos(client, app, kind):
    _, pid, base = await _setup(client)
    item = await _entry(client, base, kind)
    url = f"{base}/setting-entries/{kind}/{item['id']}/tags"
    before = await client.get(url)
    assert before.status_code == 200, before.text
    assert before.json()["setting_tags"] == [] and before.json()["categories_revision"] == 0
    saved = await client.patch(
        url,
        json={
            "expected_categories_revision": 0,
            "setting_tags": [" 同盟 ", "", "  ", "同盟", "Tag", "tag"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["setting_tags"] == ["同盟", "Tag", "tag"]
    assert saved.json()["categories_revision"] == 1
    assert "setting_tags" not in item
    for bad in (None, [False], [1], ["长" * 51], [f"标签{i}" for i in range(51)]):
        denied = await client.patch(url, json={"expected_categories_revision": 1, "setting_tags": bad})
        assert denied.status_code == 422, denied.text
    maximum = [f"{index:02}" + "界" * 48 for index in range(50)]
    saved = await client.patch(url, json={"expected_categories_revision": 1, "setting_tags": maximum})
    assert saved.status_code == 200, saved.text
    cleared = await client.patch(url, json={"expected_categories_revision": 2, "setting_tags": []})
    assert cleared.status_code == 200 and cleared.json()["setting_tags"] == []
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, pid)).categories_revision == 3


async def test_category_crud_preserves_rule_ids_until_atomic_rule_replacement(client, app):
    _, pid, base = await _setup(client)
    character = await _entry(client, base, "character")
    created = await _create(client, base, name=" 同行 ")
    category = created["item"]
    assert category["name"] == "同行" and created["categories_revision"] == 1 and created["fields_revision"] == 0
    url = f"{base}/setting-categories/{category['id']}"
    renamed = await client.patch(url, json={**await _versions(client, base), "name": "重命名", "sort": -3})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["item"]["rules"] == category["rules"]
    for invalid in ({"rules": []}, {"rules": None}, {"name": None}, {"name": "  "}, {"sort": None}):
        denied = await client.patch(url, json={**await _versions(client, base), **invalid})
        assert denied.status_code in (400, 422), denied.text
    changed = await client.patch(
        url,
        json={
            **await _versions(client, base),
            "match_mode": "any",
            "rules": [{"kind": "tag", "tag": "新标签"}, {"kind": "tag", "tag": "同盟"}],
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["categories_revision"] == 3
    assert category["rules"][0]["id"] not in {rule["id"] for rule in changed.json()["item"]["rules"]}
    read = await client.get(url)
    assert read.status_code == 200 and read.json() == changed.json()
    duplicate = await client.post(
        f"{base}/setting-categories",
        json={
            **await _versions(client, base),
            **_spec(name="重命名"),
        },
    )
    assert duplicate.status_code == 409, duplicate.text
    removed = await client.delete(url, params={"expected_categories_revision": 3})
    assert removed.status_code == 200, removed.text
    assert removed.json()["categories_revision"] == 4 and removed.json()["fields_revision"] == 0
    assert (await client.get(f"{base}/characters/{character['id']}")).status_code == 200
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(SettingCategoryRule)) == 0
        assert (await db.get(Project, pid)).categories_revision == 4


async def test_category_versions_one_winner_and_failed_rules_leave_every_row_unchanged(client, app):
    _, pid, base = await _setup(client)
    created = await _create(client, base)
    url = f"{base}/setting-categories/{created['item']['id']}"
    versions = await _versions(client, base)
    responses = await asyncio.gather(*[client.patch(url, json={**versions, "name": name}) for name in ("甲", "乙")])
    assert sorted(response.status_code for response in responses) == [200, 409]
    stale = next(response for response in responses if response.status_code == 409).json()
    assert stale["code"] == "stale_revision" and stale["scope"] == "categories"
    before = (await client.get(url)).json()
    invalid = await client.patch(
        url,
        json={
            **await _versions(client, base),
            "name": "不保留",
            "rules": [
                {"kind": "tag", "tag": "合法"},
                {"kind": "dropdown", "definition_id": 999999, "option_id": 999999},
            ],
        },
    )
    assert invalid.status_code == 404, invalid.text
    assert (await client.get(url)).json() == before
    await _dropdown(client, base)
    field_stale = await client.patch(url, json={**versions, "expected_categories_revision": 2, "name": "不保留"})
    assert field_stale.status_code == 409 and field_stale.json()["scope"] == "fields"
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, pid)).categories_revision == 2


async def test_readonly_preview_members_and_cross_project_isolation(client, app, make_client, system_admin):
    _, pid, base = await _setup(client)
    character = await _entry(client, base, "character")
    category = await _create(client, base)
    viewer = make_client()
    reader = await register(viewer, "category_reader")
    role = await create_role(system_admin, "分类只读", ["projects.read"])
    await assign_roles(system_admin, reader["id"], [role["id"]])
    preview = await viewer.post(f"{base}/setting-categories/preview", json={"draft": _spec()})
    assert preview.status_code == 200 and preview.json()["total"] == 0, preview.text
    cid = category["item"]["id"]
    assert (await viewer.get(f"{base}/setting-categories/{cid}/entries")).status_code == 200
    tags_url = f"{base}/setting-entries/character/{character['id']}/tags"
    assert (await viewer.get(tags_url)).status_code == 200
    writes = [
        ("POST", f"{base}/setting-categories", {**await _versions(client, base), **_spec(name="禁止")}),
        ("PATCH", f"{base}/setting-categories/{cid}", {**await _versions(client, base), "name": "禁止"}),
        ("PATCH", tags_url, {"expected_categories_revision": 1, "setting_tags": ["禁止"]}),
    ]
    for method, url, body in writes:
        denied = await viewer.request(method, url, json=body)
        assert denied.status_code == 403, denied.text
    assert (
        await viewer.delete(f"{base}/setting-categories/{cid}", params={"expected_categories_revision": 1})
    ).status_code == 403
    outsider = make_client()
    await register(outsider, "category_outsider")
    for suffix in (
        "setting-categories",
        f"setting-categories/{cid}",
        "setting-entry-tags",
        f"setting-categories/{cid}/entries",
        f"setting-entries/character/{character['id']}/tags",
    ):
        assert (await outsider.get(f"{base}/{suffix}")).status_code == 404
    assert (await outsider.post(f"{base}/setting-categories/preview", json={"draft": _spec()})).status_code == 404
    other = await create_project(client, title="另书")
    foreign_base = f"/api/v1/projects/{other['id']}"
    assert (await client.get(f"{foreign_base}/setting-categories/{cid}")).status_code == 404
    assert (await client.get(f"{foreign_base}/setting-entries/character/{character['id']}/tags")).status_code == 404
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, pid)).categories_revision == 1
        assert await db.scalar(select(func.count()).select_from(SettingTextField)) == 0


async def test_archived_existing_rules_are_retained_only_by_server_owned_category(client):
    _, _, base = await _setup(client)
    did, oid = await _dropdown(client, base)
    rule = {"kind": "dropdown", "definition_id": did, "option_id": oid}
    made = await _create(client, base, rules=[rule])
    cid = made["item"]["id"]
    response = await client.patch(
        f"{base}/setting-field-definitions/{did}",
        json={
            "expected_fields_revision": 2,
            "archived": True,
        },
    )
    assert response.status_code == 200, response.text
    kept = await client.patch(
        f"{base}/setting-categories/{cid}", json={**await _versions(client, base), "rules": [rule]}
    )
    assert kept.status_code == 200, kept.text
    new = await client.post(
        f"{base}/setting-categories", json={**await _versions(client, base), **_spec(name="新类", rules=[rule])}
    )
    assert new.status_code == 409 and new.json()["code"] == "category_rule_archived"
    plain = await client.post(f"{base}/setting-categories/preview", json={"draft": _spec(rules=[rule])})
    assert plain.status_code == 409, plain.text
    edited = await client.post(
        f"{base}/setting-categories/preview",
        json={
            "draft": _spec(rules=[rule]),
            "editing_category_id": cid,
        },
    )
    assert edited.status_code == 200, edited.text
    forged = await client.post(
        f"{base}/setting-categories/preview",
        json={
            "draft": _spec(rules=[rule]),
            "retained_rules": [rule],
        },
    )
    assert forged.status_code == 422


@pytest.mark.parametrize("operation", ["delete_definition", "change_type", "shrink_scope", "delete_option"])
async def test_rule_only_references_protect_field_configuration_without_version_drift(client, app, operation):
    _, pid, base = await _setup(client)
    did, oid = await _dropdown(client, base, types=["character", "organization"])
    await _create(client, base, rules=[{"kind": "dropdown", "definition_id": did, "option_id": oid}])
    url = f"{base}/setting-field-definitions/{did}"
    before = (await client.get(url)).json()
    if operation == "delete_definition":
        response = await client.delete(url, params={"expected_fields_revision": 2})
    elif operation == "delete_option":
        response = await client.delete(f"{url}/options/{oid}", params={"expected_fields_revision": 2})
    else:
        response = await client.patch(
            url,
            json={
                "expected_fields_revision": 2,
                **({"field_type": "line"} if operation == "change_type" else {"applicable_types": ["organization"]}),
            },
        )
    assert response.status_code == 409, response.text
    assert response.json()["code"] in ("field_in_use", "option_in_use", "incompatible_field_scope")
    assert (await client.get(url)).json() == before
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, pid)
        assert (project.categories_revision, project.fields_revision) == (1, 2)
        assert await db.scalar(select(func.count()).select_from(SettingTextField)) == 0


async def test_category_query_updates_any_all_with_current_tags_and_delete_keeps_entries(client):
    _, _, base = await _setup(client)
    a = await _entry(client, base, "character", "甲")
    b = await _entry(client, base, "character", "乙")
    for item, tags in ((a, ["同盟"]), (b, ["同盟", "队长"])):
        response = await client.patch(
            f"{base}/setting-entries/character/{item['id']}/tags",
            json={
                "expected_categories_revision": (await _versions(client, base))["expected_categories_revision"],
                "setting_tags": tags,
            },
        )
        assert response.status_code == 200, response.text
    category = await _create(client, base, rules=[{"kind": "tag", "tag": "同盟"}, {"kind": "tag", "tag": "队长"}])
    url = f"{base}/setting-categories/{category['item']['id']}"
    assert (await client.get(f"{url}/entries")).json()["total"] == 1
    assert (
        await client.post(
            f"{base}/setting-categories/preview",
            json={
                "draft": _spec(
                    match_mode="any",
                    rules=[{"kind": "tag", "tag": "同盟"}, {"kind": "tag", "tag": "队长"}],
                )
            },
        )
    ).json()["total"] == 2
    assert (await client.get(f"{url}/entries", params={"chapter_id": 999999})).status_code in (400, 404)
    assert (await client.get(f"{url}/entries", params={"chapter_no": 5, "chapter_id": 1})).status_code == 422


async def test_suggestions_are_literal_distinct_and_bounded_in_sql(client, app):
    _, pid, base = await _setup(client)
    async with app.state.sessionmaker() as db:
        db.add_all(Character(project_id=pid, name=f"人物{i}", setting_tags=["共享", f"标签{i:03}"]) for i in range(160))
        db.add(Character(project_id=pid, name="特殊", setting_tags=["A", "a", "百分%", "普通"]))
        await db.commit()
    statements = []

    def capture(_conn, _cursor, statement, params, _context, _many):
        statements.append(statement)

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await client.get(f"{base}/setting-entry-tags", params={"limit": 7})
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", capture)
    assert result.status_code == 200, result.text
    assert len(result.json()["items"]) == 7 and result.json()["has_more"] is True
    tag_sql = [statement for statement in statements if "json_each" in statement]
    assert len(tag_sql) == 1 and "LIMIT" in tag_sql[0].upper()
    assert (await client.get(f"{base}/setting-entry-tags", params={"q": "%"})).json()["items"] == ["百分%"]
    case = (await client.get(f"{base}/setting-entry-tags", params={"q": "A"})).json()["items"]
    assert case == ["A", "a"]
    assert (await client.get(f"{base}/setting-entry-tags", params={"limit": 101})).status_code == 422


async def test_admin_writes_capture_real_actor_and_safe_field_names_without_labels(client, app, system_admin):
    _, pid, base = await _setup(client)
    character = await _entry(client, base, "character")
    await _create(system_admin, base, name="不进审计的私密分类", rules=[{"kind": "tag", "tag": "私密标签"}])
    response = await system_admin.patch(
        f"{base}/setting-entries/character/{character['id']}/tags",
        json={
            "expected_categories_revision": 1,
            "setting_tags": ["私密标签"],
        },
    )
    assert response.status_code == 200, response.text
    admin = (await system_admin.get("/api/v1/auth/me")).json()
    async with app.state.sessionmaker() as db:
        rows = list(
            await db.scalars(select(AdminAuditLog).where(AdminAuditLog.project_id == pid).order_by(AdminAuditLog.id))
        )
        assert [row.summary["changed_fields"] for row in rows] == [["setting_categories"], ["setting_tags"]]
        assert all(row.actor_user_id == admin["id"] and row.action == "project.update" for row in rows)
        assert "私密标签" not in str([row.summary for row in rows])


@pytest.mark.parametrize(
    "damage", ["tags_whitespace", "tags_duplicate", "foreign_option", "wrong_type", "no_intersection", "empty_rules"]
)
async def test_category_graph_rejects_raw_invalid_data_without_writing(client, app, damage):
    from app.services import setting_category_service as service

    _, pid, base = await _setup(client)
    character = await _entry(client, base, "character")
    did, oid = await _dropdown(client, base)
    made = await _create(client, base, rules=[{"kind": "dropdown", "definition_id": did, "option_id": oid}])
    async with app.state.sessionmaker() as db:
        if damage.startswith("tags_"):
            (await db.get(Character, character["id"])).setting_tags = (
                [" 脏 "] if damage == "tags_whitespace" else ["重复", "重复"]
            )
        elif damage == "wrong_type":
            (await db.get(SettingFieldDefinition, did)).field_type = "line"
        elif damage == "no_intersection":
            (await db.get(SettingFieldDefinition, did)).applicable_types = ["organization"]
        elif damage == "empty_rules":
            for rule in await db.scalars(
                select(SettingCategoryRule).where(SettingCategoryRule.category_id == made["item"]["id"])
            ):
                await db.delete(rule)
        else:
            other = SettingFieldDefinition(
                project_id=pid, label="别组", field_type="dropdown", applicable_types=["character"]
            )
            db.add(other)
            await db.flush()
            (await db.get(SettingFieldOption, oid)).definition_id = other.id
        await db.flush()
        pending = Character(project_id=pid, name="不应被验证写入")
        db.add(pending)
        statements = []

        def capture(_conn, _cursor, statement, params, _context, _many):
            statements.append(statement)

        event.listen(app.state.engine.sync_engine, "before_cursor_execute", capture)
        try:
            with pytest.raises(AppError):
                await service.validate_project_category_graph(db, pid)
            assert pending.id is None
            assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
        finally:
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", capture)
            await db.rollback()


@pytest.mark.parametrize("operation", ["create", "patch", "delete", "tags"])
async def test_write_failure_after_claim_rolls_back_rules_tags_and_revision(client, app, monkeypatch, operation):
    from app.services import setting_category_service as service

    _, pid, base = await _setup(client)
    character = await _entry(client, base, "character")
    category = await _create(client, base)
    cid = category["item"]["id"]
    original = service.claim_categories_revision

    async def fail_after_claim(*args):
        await original(*args)
        raise ConflictError("测试事务失败")

    monkeypatch.setattr(service, "claim_categories_revision", fail_after_claim)
    if operation == "tags":
        failed = await client.patch(
            f"{base}/setting-entries/character/{character['id']}/tags",
            json={
                "expected_categories_revision": 1,
                "setting_tags": ["不保留"],
            },
        )
    elif operation == "delete":
        failed = await client.delete(f"{base}/setting-categories/{cid}", params={"expected_categories_revision": 1})
    elif operation == "create":
        failed = await client.post(
            f"{base}/setting-categories",
            json={
                **await _versions(client, base),
                **_spec(name="不保留"),
            },
        )
    else:
        failed = await client.patch(
            f"{base}/setting-categories/{cid}",
            json={
                **await _versions(client, base),
                "name": "不保留",
                "rules": [{"kind": "tag", "tag": "不保留"}],
            },
        )
    assert failed.status_code == 409, failed.text
    assert (await client.get(f"{base}/setting-categories/{cid}")).json() == category
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, pid)).categories_revision == 1
        assert (await db.get(Character, character["id"])).setting_tags == []
        assert await db.scalar(select(func.count()).select_from(SettingCategory)) == 1
        assert await db.scalar(select(func.count()).select_from(SettingCategoryRule)) == 1


async def test_tag_composition_helper_only_flushes_and_graph_preserves_valid_archived_rules(client, app):
    from app.services import project_lifecycle
    from app.services import setting_category_service as service

    _, pid, base = await _setup(client)
    character = await _entry(client, base, "character")
    did, oid = await _dropdown(client, base)
    await _create(client, base, rules=[{"kind": "dropdown", "definition_id": did, "option_id": oid}])
    async with app.state.sessionmaker() as db:
        await project_lifecycle.lock_active(db, pid)
        (await db.get(SettingFieldDefinition, did)).archived = True
        (await db.get(SettingFieldOption, oid)).archived = True
        await db.flush()
        tags = await service.apply_entry_tags(db, pid, "character", character["id"], [" 规范 ", "规范", ""])
        assert tags == ["规范"]
        before = (await db.get(Project, pid)).categories_revision
        await service.validate_project_category_graph(db, pid)
        assert (await db.get(Project, pid)).categories_revision == before == 1
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert (await db.get(Character, character["id"])).setting_tags == []
        assert (await db.get(SettingFieldDefinition, did)).archived is False


async def test_legal_scope_change_rename_and_archive_preserve_rule_identity(client):
    _, _, base = await _setup(client)
    did, oid = await _dropdown(client, base, types=["character", "organization"])
    made = await _create(
        client,
        base,
        applicable_types=["character", "organization"],
        rules=[
            {"kind": "dropdown", "definition_id": did, "option_id": oid},
        ],
    )
    changed = await client.patch(
        f"{base}/setting-field-definitions/{did}",
        json={
            "expected_fields_revision": 2,
            "label": "当前阵营",
            "sort": 9,
            "applicable_types": ["organization"],
        },
    )
    assert changed.status_code == 200, changed.text
    changed = await client.patch(
        f"{base}/setting-field-definitions/{did}/options/{oid}",
        json={
            "expected_fields_revision": 3,
            "label": "新的名字",
            "archived": True,
        },
    )
    assert changed.status_code == 200, changed.text
    cid = made["item"]["id"]
    current = (await client.get(f"{base}/setting-categories/{cid}")).json()
    assert current["item"]["rules"] == made["item"]["rules"]
    denied = await client.patch(
        f"{base}/setting-categories/{cid}",
        json={
            **await _versions(client, base),
            "applicable_types": ["character"],
        },
    )
    assert denied.status_code == 400 and denied.json()["code"] == "incompatible_category_rule"
    assert (await client.get(f"{base}/setting-categories/{cid}")).json() == current


@pytest.mark.parametrize(
    "damage", ["foreign_definition", "tags_object", "tags_null", "too_many_rules", "tag_rule_whitespace"]
)
async def test_graph_checks_additional_import_style_damage(client, app, damage):
    from app.services import setting_category_service as service

    _, pid, base = await _setup(client)
    character = await _entry(client, base, "character")
    made = await _create(client, base)
    async with app.state.sessionmaker() as db:
        cid = made["item"]["id"]
        if damage == "foreign_definition":
            other = Project(user_id=(await db.get(Project, pid)).user_id, title="另书")
            db.add(other)
            await db.flush()
            definition = SettingFieldDefinition(
                project_id=other.id, label="另书", field_type="dropdown", applicable_types=["character"]
            )
            db.add(definition)
            await db.flush()
            option = SettingFieldOption(definition_id=definition.id, label="另书")
            db.add(option)
            await db.flush()
            db.add(
                SettingCategoryRule(category_id=cid, kind="dropdown", definition_id=definition.id, option_id=option.id)
            )
        elif damage.startswith("tags_"):
            (await db.get(Character, character["id"])).setting_tags = (
                {"bad": "shape"} if damage == "tags_object" else None
            )
        elif damage == "too_many_rules":
            db.add_all(SettingCategoryRule(category_id=cid, kind="tag", tag=f"规则{i}") for i in range(20))
        else:
            rule = await db.scalar(select(SettingCategoryRule).where(SettingCategoryRule.category_id == cid))
            rule.tag = " 非规范 "
        await db.flush()
        with pytest.raises(AppError):
            await service.validate_project_category_graph(db, pid)
        await db.rollback()


@pytest.mark.parametrize("read_kind", ["list", "detail", "tags"])
async def test_configuration_reads_keep_versions_and_payload_in_one_real_snapshot(
    snapshot_database, monkeypatch, read_kind
):
    from app.services import setting_category_service as service

    maker, seeded = snapshot_database
    reached, release = asyncio.Event(), asyncio.Event()
    original = service.categories_revision if read_kind == "tags" else query._revisions
    armed = True

    async def pause_after_version(*args):
        nonlocal armed
        result = await original(*args)
        if armed:
            armed = False
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(
        service if read_kind == "tags" else query,
        "categories_revision" if read_kind == "tags" else "_revisions",
        pause_after_version,
    )

    async def read():
        if read_kind == "list":
            return await service.list_categories(maker, seeded.actor, seeded.pid)
        if read_kind == "detail":
            return await service.read_category(maker, seeded.actor, seeded.pid, seeded.category)
        return await service.read_tags(maker, seeded.actor, seeded.pid, "character", seeded.characters[0])

    task = asyncio.create_task(read())
    await asyncio.wait_for(reached.wait(), timeout=10)
    try:
        async with maker() as db:
            await db.execute(
                update(Project).where(Project.id == seeded.pid).values(categories_revision=1, fields_revision=1)
            )
            await db.execute(
                update(SettingCategory).where(SettingCategory.id == seeded.category).values(name="新的分类")
            )
            await db.execute(
                update(Character).where(Character.id == seeded.characters[0]).values(setting_tags=["新标签"])
            )
            await db.execute(
                update(SettingCategoryRule)
                .where(SettingCategoryRule.category_id == seeded.category, SettingCategoryRule.kind == "tag")
                .values(tag="新标签")
            )
            await db.commit()
    finally:
        release.set()
    before = await task
    assert before.categories_revision == 0
    if read_kind == "tags":
        assert before.setting_tags == ["同盟"]
    else:
        assert before.fields_revision == 0
        item = before.items[0] if read_kind == "list" else before.item
        assert item.name == "同行" and item.rules[0].tag == "同盟"
    after = await read()
    assert after.categories_revision == 1
    if read_kind == "tags":
        assert after.setting_tags == ["新标签"]
    else:
        assert after.fields_revision == 1
        assert (after.items[0] if read_kind == "list" else after.item).name == "新的分类"


async def test_supported_databases_category_cas_and_bounded_tag_queries(snapshot_database):
    from app.schemas import setting_categories as schemas
    from app.services import setting_category_service as service

    maker, seeded = snapshot_database
    data = schemas.CategoryCreate(expected_categories_revision=0, expected_fields_revision=0, **_spec(name="并发"))

    async def write():
        async with maker() as db:
            db.info["authenticated_actor"] = seeded.actor
            try:
                return await service.save_category(db, seeded.actor.id, seeded.pid, data)
            except ConflictError as exc:
                return exc

    results = await asyncio.gather(write(), write())
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    assert all(not isinstance(result, ConflictError) or result.extra["scope"] == "categories" for result in results)
    listed = await service.list_categories(maker, seeded.actor, seeded.pid, types=["character"], q="并发")
    assert listed.total == 1 and listed.categories_revision == 1
    suggested = await service.suggest_tags(maker, seeded.actor, seeded.pid, limit=1)
    assert suggested.items == ["同盟"] and suggested.has_more is False


async def test_config_reads_revalidate_token_and_lifecycle_and_emit_no_writes(client, app):
    from app.services import setting_category_service as service

    user, pid, base = await _setup(client)
    character = await _entry(client, base, "character")
    await _create(client, base)
    async with app.state.sessionmaker() as db:
        actor = AuthenticatedActor(user["id"], (await db.get(User, user["id"])).token_version)
    with pytest.raises(UnauthorizedError):
        await service.list_categories(
            app.state.sessionmaker, AuthenticatedActor(actor.id, actor.token_version + 1), pid
        )
    statements = []

    def capture(_conn, _cursor, statement, params, _context, _many):
        statements.append(statement)

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await service.list_categories(app.state.sessionmaker, actor, pid)
        await service.read_tags(app.state.sessionmaker, actor, pid, "character", character["id"])
        await service.suggest_tags(app.state.sessionmaker, actor, pid)
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", capture)
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
    assert (await client.delete(base)).status_code == 204
    for suffix in ("setting-categories", "setting-entry-tags", f"setting-entries/character/{character['id']}/tags"):
        assert (await client.get(f"{base}/{suffix}")).status_code == 404
    assert (await client.post(f"{base}/setting-categories/preview", json={"draft": _spec()})).status_code == 404
