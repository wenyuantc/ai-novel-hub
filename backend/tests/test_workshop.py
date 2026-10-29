"""工坊跨用户提交、审核、导入与权限的行为回归。"""

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.ai.prompts import registry
from app.ai.templating import TemplateVar
from app.models import User
from tests.helpers import register

ROOT = "/api/v1/workshop"
PROMPTS = "/api/v1/prompt-templates"


@pytest.fixture
async def people(app, make_client):
    author, reader, admin = (make_client() for _ in range(3))
    await register(author, "author")
    await register(reader, "reader")
    await register(admin, "reviewer")
    async with app.state.sessionmaker() as db:
        user = await db.scalar(select(User).where(User.username == "reviewer"))
        user.is_admin = True
        await db.commit()
    return author, reader, admin


def style(**values):
    return {
        "kind": "style",
        "name": "风格分享",
        "description": "工坊说明",
        "category": "general",
        "tags": ["中文", "a%_"],
        "content": {"tone": "轻快", "extra": "短句为主"},
        **values,
    }


def prompt(**values):
    tpl = registry.TEMPLATES["chapter.generate"]
    return {
        "kind": "prompt",
        "name": "模板分享",
        "prompt_key": tpl.key,
        "content": {"system": tpl.system + "\n我的要求", "user": tpl.user},
        **values,
    }


async def submit(client, data):
    response = await client.post(ROOT + "/submissions", json=data)
    assert response.status_code == 201, response.text
    return response.json()


async def approve(admin, submission, **changes):
    response = await admin.post(ROOT + f"/admin/submissions/{submission['id']}/approve", json=changes)
    assert response.status_code == 200, response.text
    return response.json()["item_id"]


async def test_review_import_like_update_lifecycle(people):
    author, reader, admin = people
    submission = await submit(author, style())
    assert (await reader.get(ROOT + f"/submissions/{submission['id']}")).status_code == 404
    assert (await reader.get(ROOT + "/admin/submissions")).status_code == 403
    item_id = await approve(admin, submission, category="scifi", tags=["中文", "a%_"])
    for params in ({"kind": "style"}, {"category": "scifi"}, {"tag": "中文"}, {"tag": "a%_"}, {"q": "工坊说明"}):
        response = await reader.get(ROOT + "/items", params=params)
        assert response.json()["total"] == 1
    assert (await reader.get(ROOT + "/items", params={"tag": "aXX"})).json()["total"] == 0
    for _ in range(2):
        liked = await reader.post(ROOT + f"/items/{item_id}/like")
        assert liked.json()["like_count"] == 1
    copies = []
    for _ in range(2):
        imported = await reader.post(ROOT + f"/items/{item_id}/import")
        assert imported.status_code == 200, imported.text
        assert imported.json()["download_count"] == 1
        copies.append(imported.json()["style_id"])
    assert copies[0] != copies[1]
    styles = (await reader.get("/api/v1/styles")).json()
    assert {s["name"] for s in styles if s["id"] in copies} == {"风格分享", "风格分享（工坊）"}
    update = await submit(author, style(item_id=item_id, content={"tone": "冷静"}))
    assert (await author.post(ROOT + "/submissions", json=style(item_id=item_id))).status_code == 409
    old = (await reader.get(ROOT + f"/items/{item_id}")).json()
    assert old["content"]["tone"] == "轻快"
    await approve(admin, update)
    new = (await reader.get(ROOT + f"/items/{item_id}")).json()
    assert (new["content"]["tone"], new["like_count"], new["download_count"]) == ("冷静", 1, 1)
    for _ in range(2):
        assert (await reader.delete(ROOT + f"/items/{item_id}/like")).json()["like_count"] == 0
    assert (await author.post(ROOT + f"/items/{item_id}/hide")).status_code == 200
    assert (await reader.get(ROOT + f"/items/{item_id}")).status_code == 404
    assert (await author.post(ROOT + f"/items/{item_id}/unhide")).status_code == 200
    assert (await admin.post(ROOT + f"/items/{item_id}/hide")).status_code == 200
    assert (await author.post(ROOT + f"/items/{item_id}/unhide")).status_code == 403
    assert (await admin.post(ROOT + f"/items/{item_id}/unhide")).status_code == 200


async def test_prompt_import_keeps_history_and_upgrade_failure(people, monkeypatch):
    author, reader, admin = people
    item_id = await approve(admin, await submit(author, prompt()))
    tpl = registry.TEMPLATES["chapter.generate"]
    old = {"system": tpl.system + "\n之前版本", "user": tpl.user}
    assert (await reader.put(PROMPTS + "/chapter.generate", json=old)).status_code == 200
    response = await reader.post(ROOT + f"/items/{item_id}/import")
    assert response.status_code == 200, response.text
    versions = (await reader.get(PROMPTS + "/chapter.generate/versions")).json()
    assert versions[0]["note"].startswith("从工坊导入：")
    previous = next(v for v in versions if v["system"] == old["system"])
    restored = await reader.post(PROMPTS + f"/chapter.generate/versions/{previous['id']}/restore")
    assert restored.json()["override"]["system"] == old["system"]
    monkeypatch.setitem(
        registry.TEMPLATES,
        tpl.key,
        replace(
            tpl,
            variables=(
                *tpl.variables,
                TemplateVar("new_locked", "新增锁定", "text", required=True, locked=True, sample="锁定"),
            ),
        ),
    )
    failed = await reader.post(ROOT + f"/items/{item_id}/import")
    assert failed.status_code == 400 and failed.json()["errors"]
    assert (await reader.get(PROMPTS + "/chapter.generate")).json()["override"]["system"] == old["system"]


async def test_submission_validation_and_rejection(people):
    from app.models.style_presets import PRESET_STYLES
    from app.services.style_service import FIELD_LIMITS

    author, reader, admin = people
    preset = {f: PRESET_STYLES[0].get(f, "") for f in FIELD_LIMITS}
    response = await author.post(ROOT + "/submissions", json=style(content=preset))
    assert response.status_code == 400 and "预设" in response.json()["detail"]
    tpl = registry.TEMPLATES["chapter.generate"]
    response = await author.post(ROOT + "/submissions", json=prompt(content={"system": tpl.system, "user": tpl.user}))
    assert response.status_code == 400 and "系统默认" in response.json()["detail"]
    locked_tpl = registry.TEMPLATES["analysis.chapter"]
    response = await author.post(
        ROOT + "/submissions", json=prompt(prompt_key=locked_tpl.key, content={"system": "", "user": "缺少锁定变量"})
    )
    assert response.status_code == 400 and response.json()["errors"]
    for invalid in (
        style(name="  "),
        style(tags=['bad"tag']),
        style(tags=["x"] * 6),
        style(category="wrong"),
        style(content={"name": "伪造"}),
        style(content={"extra": "x" * 5001}),
        prompt(prompt_key="missing"),
    ):
        assert (await author.post(ROOT + "/submissions", json=invalid)).status_code in (400, 422)
    submission = await submit(author, style())
    url = ROOT + f"/admin/submissions/{submission['id']}"
    assert (await admin.post(url + "/reject", json={"note": "  "})).status_code == 422
    rejected = await admin.post(url + "/reject", json={"note": "请完善内容"})
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["review_note"] == "请完善内容"
    assert (await admin.post(url + "/approve", json={})).status_code == 409
    assert (await author.post(ROOT + f"/submissions/{submission['id']}/withdraw")).status_code == 409
    assert (await reader.get(ROOT + f"/submissions/{submission['id']}")).status_code == 404
    withdrawn = await submit(author, style())
    assert (await author.post(ROOT + f"/submissions/{withdrawn['id']}/withdraw")).json()["status"] == "cancelled"
    assert (await author.get(ROOT + "/submissions", params={"status": "rejected"})).json()["total"] == 1


async def test_admin_official_and_permissions(people):
    author, reader, admin = people
    official = await submit(admin, style())
    assert official["status"] == "approved"
    item_id = official["item_id"]
    item = (await reader.get(ROOT + f"/items/{item_id}")).json()
    assert item["is_official"] is True
    direct = await admin.post(ROOT + "/admin/items", json=prompt())
    assert direct.status_code == 201 and direct.json()["is_official"]
    own = await approve(admin, await submit(author, style(name="用户条目")))
    assert not (await reader.get(ROOT + f"/items/{own}")).json()["is_official"]
    patch = await admin.patch(ROOT + f"/admin/items/{own}", json={"category": "wuxia", "tags": [" 新 ", "新"]})
    assert patch.json()["category"] == "wuxia" and patch.json()["tags"] == ["新"]
    assert (await admin.patch(ROOT + f"/admin/items/{own}", json={"content": {"tone": "修改"}})).status_code == 422
    for method, path, data in (
        ("get", "/admin/submissions", None),
        ("post", "/admin/items", style()),
        ("post", f"/admin/submissions/{official['id']}/approve", {}),
        ("post", f"/admin/submissions/{official['id']}/reject", {"note": "驳回"}),
        ("patch", f"/admin/items/{own}", {}),
        ("delete", f"/admin/items/{own}", None),
    ):
        response = await reader.request(method, ROOT + path, **({"json": data} if data is not None else {}))
        assert response.status_code == 403
    assert (await reader.post(ROOT + "/submissions", json=style(item_id=own))).status_code == 404
    assert (await reader.post(ROOT + f"/items/{own}/hide")).status_code == 404
    assert (await admin.delete(ROOT + f"/admin/items/{own}")).status_code == 204
    assert (await author.get(ROOT + f"/items/{own}")).status_code == 404


async def test_hidden_review_cannot_bypass_admin_or_unhide_implicitly(people):
    author, reader, admin = people
    item_id = await approve(admin, await submit(author, style()))
    await author.post(ROOT + f"/items/{item_id}/hide")
    update = await submit(author, style(item_id=item_id, content={"extra": "新版"}))
    await approve(admin, update)
    assert (await author.get(ROOT + f"/items/{item_id}")).json()["hidden_by"] == "author"
    assert (await reader.get(ROOT + f"/items/{item_id}")).status_code == 404
    await admin.post(ROOT + f"/items/{item_id}/hide")
    assert (await admin.get(ROOT + "/items")).json()["total"] == 1
    assert (await reader.get(ROOT + "/items")).json()["total"] == 0
    await author.post(ROOT + f"/items/{item_id}/hide")
    assert (await author.post(ROOT + f"/items/{item_id}/unhide")).status_code == 403
    for suffix in ("like", "import"):
        assert (await reader.post(ROOT + f"/items/{item_id}/{suffix}")).status_code == 404
    assert (await author.get(ROOT + "/mine/items")).json()["total"] == 1
    await admin.post(ROOT + f"/items/{item_id}/unhide")
    rejected = await submit(author, style(item_id=item_id, content={"extra": "拒绝的版本"}))
    await admin.post(ROOT + f"/admin/submissions/{rejected['id']}/reject", json={"note": "不通过"})
    assert (await reader.get(ROOT + f"/items/{item_id}")).json()["content"]["extra"] == "新版"


async def test_anonymous_delete_author_keeps_items_counts(people, app):
    from app.models import WorkshopSubmission

    author, reader, admin = people
    item_id = await approve(admin, await submit(author, style(is_anonymous=True)))
    item = (await reader.get(ROOT + f"/items/{item_id}")).json()
    assert item["author_name"] is None and item["author_username"] is None
    assert "author_id" not in item and "submitter_id" not in item
    assert (await author.get(ROOT + f"/items/{item_id}")).json()["author_name"] == "author"
    assert (await admin.get(ROOT + f"/items/{item_id}")).json()["author_username"] == "author"
    await reader.post(ROOT + f"/items/{item_id}/like")
    await reader.post(ROOT + f"/items/{item_id}/import")
    pending = await submit(author, style(item_id=item_id))
    user_id = (await author.get("/api/v1/auth/me")).json()["id"]
    assert (await admin.delete(f"/api/v1/admin/users/{user_id}")).status_code == 204
    item = (await reader.get(ROOT + f"/items/{item_id}")).json()
    assert item["author_name"] == "已注销用户"
    assert item["like_count"] == item["download_count"] == 1
    async with app.state.sessionmaker() as db:
        assert await db.get(WorkshopSubmission, pending["id"]) is None


@pytest.mark.parametrize("prior", ["none", "disabled", "legacy"])
async def test_import_preserves_effective_and_disabled_and_legacy_content(people, app, prior):
    from app.models import PromptOverrideVersion, PromptTemplateOverride

    author, reader, admin = people
    tpl = registry.TEMPLATES["chapter.generate"]
    item_id = await approve(admin, await submit(author, prompt()))
    if prior != "none":
        await reader.put(PROMPTS + "/chapter.generate", json={"system": tpl.system + "停用正文", "user": tpl.user})
    if prior == "disabled":
        await reader.patch(PROMPTS + "/chapter.generate", json={"enabled": False})
    if prior == "legacy":
        async with app.state.sessionmaker() as db:
            row = await db.scalar(select(PromptTemplateOverride).where(PromptTemplateOverride.key == tpl.key))
            row.system = tpl.system + "未写历史的正文"
            await db.commit()
    for _ in range(2):
        response = await reader.post(ROOT + f"/items/{item_id}/import")
        assert response.status_code == 200 and response.json()["download_count"] == 1
    versions = (await reader.get(PROMPTS + "/chapter.generate/versions")).json()
    assert sum(v["note"].startswith("从工坊导入：") for v in versions) == 2
    assert (await reader.get(PROMPTS + "/chapter.generate")).json()["override"]["enabled"]
    expected = tpl.system + "未写历史的正文" if prior == "legacy" else tpl.system
    recover = next(v for v in versions if v["system"] == expected)
    if prior == "disabled":
        assert any(v["system"] == tpl.system + "停用正文" for v in versions)
    restored = await reader.post(PROMPTS + f"/chapter.generate/versions/{recover['id']}/restore")
    assert restored.json()["override"]["system"] == expected
    async with app.state.sessionmaker() as db:
        assert len((await db.scalars(select(PromptOverrideVersion))).all()) <= 20


async def test_concurrent_likes_and_review_are_atomic(people, app):
    from sqlalchemy import func

    from app.models import WorkshopItem, WorkshopLike

    author, reader, admin = people
    submission = await submit(author, style())
    approvals = await asyncio.gather(
        *(admin.post(ROOT + f"/admin/submissions/{submission['id']}/approve", json={}) for _ in range(3))
    )
    assert sorted(r.status_code for r in approvals) == [200, 409, 409]
    item_id = next(r.json()["item_id"] for r in approvals if r.status_code == 200)
    responses = await asyncio.gather(
        *(c.post(ROOT + f"/items/{item_id}/like") for c in (author, reader, admin, reader, author, admin))
    )
    assert all(r.status_code == 200 for r in responses)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(WorkshopItem.id))) == 1
        assert await db.scalar(select(func.count()).select_from(WorkshopLike)) == 3
        assert await db.scalar(select(WorkshopItem.like_count).where(WorkshopItem.id == item_id)) == 3
    await asyncio.gather(*(reader.delete(ROOT + f"/items/{item_id}/like") for _ in range(3)))
    assert (await reader.get(ROOT + f"/items/{item_id}")).json()["like_count"] == 2


async def test_missing_key_import_does_not_count(people, monkeypatch):
    author, reader, admin = people
    item_id = await approve(admin, await submit(author, prompt()))
    monkeypatch.setattr(registry, "TEMPLATES", {k: v for k, v in registry.TEMPLATES.items() if k != "chapter.generate"})
    assert (await reader.post(ROOT + f"/items/{item_id}/import")).status_code == 400
    item = (await reader.get(ROOT + f"/items/{item_id}")).json()
    assert item["download_count"] == 0 and not item["imported"]


async def test_review_update_and_delete_leave_consistent_state(people, app):
    from sqlalchemy import func

    from app.models import WorkshopItem, WorkshopSubmission

    author, reader, admin = people
    # SQLite 能覆盖接口竞争终态，但不模拟 PostgreSQL 的行锁等待；PG 锁顺序另由实现保证。
    for _ in range(3):
        item_id = await approve(admin, await submit(author, style()))
        pending = await submit(author, style(item_id=item_id, content={"extra": "更新正文"}))
        reviewed, deleted = await asyncio.gather(
            admin.post(ROOT + f"/admin/submissions/{pending['id']}/approve", json={}),
            admin.delete(ROOT + f"/admin/items/{item_id}"),
        )
        assert reviewed.status_code in (200, 404, 409), reviewed.text
        assert deleted.status_code == 204, deleted.text
        assert (await reader.get(ROOT + f"/items/{item_id}")).status_code == 404
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(func.count()).select_from(WorkshopItem)) == 0
            assert await db.scalar(select(func.count()).select_from(WorkshopSubmission)) == 0


async def test_pagination_sort_search_limits_and_auth(people, make_client):
    author, reader, admin = people
    for i in range(3):
        await submit(admin, style(name=f"条目{i}", tags=["标签"], description="说明%_" if i == 1 else "说明XX"))
    page = (await reader.get(ROOT + "/items", params={"page": 2, "page_size": 2})).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    exact = (await reader.get(ROOT + "/items", params={"q": "%_"})).json()
    assert exact["total"] == 1
    item_id = exact["items"][0]["id"]
    await reader.post(ROOT + f"/items/{item_id}/like")
    await reader.post(ROOT + f"/items/{item_id}/import")
    for sort in ("likes", "downloads"):
        assert (await reader.get(ROOT + "/items", params={"sort": sort})).json()["items"][0]["id"] == item_id
    for params in ({"page_size": 51}, {"page": 0}, {"sort": "wrong"}):
        assert (await reader.get(ROOT + "/items", params=params)).status_code == 422
    outsider = make_client()
    assert (await outsider.get(ROOT + "/items")).status_code == 401
    oversized = await author.post(
        ROOT + "/submissions", content=b" " * (1024 * 1024 + 1), headers={"Content-Type": "application/json"}
    )
    assert oversized.status_code == 400 and "1MB" in oversized.json()["detail"]


async def test_pending_limit(people):
    author, _, admin = people
    for i in range(20):
        await submit(author, style(name=f"待审核{i}"))
    assert (await author.post(ROOT + "/submissions", json=style())).status_code == 400
    assert (await admin.get(ROOT + "/admin/submissions")).json()["total"] == 20
