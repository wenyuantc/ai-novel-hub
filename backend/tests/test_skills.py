import io
import zipfile

import pytest
from sqlalchemy import update

from app.models import User
from app.services import skill_service
from tests.helpers import register

BASE = "/api/v1/skills"


async def make_admin(app, username: str) -> None:
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.username == username).values(is_admin=True))
        await db.commit()


def skill_body(**kw) -> dict:
    body = {
        "slug": "my-flow",
        "name": "我的工作流",
        "description": "测试用工作流",
        "category": "写作",
        "triggers": ["/流程", "Flow"],
        "body": "# 步骤\n\n1. 先想清楚\n2. 再动笔\n",
        "references": [{"title": "资料甲", "content": "甲的内容"}, {"title": "资料乙", "content": "乙的内容"}],
    }
    body.update(kw)
    return body


async def test_builtin_global_skills_listed_and_readonly(client):
    await register(client)
    items = (await client.get(BASE, params={"scope": "global"})).json()
    assert [i["builtin_key"] for i in items] == ["long-continue", "deslop", "chapter-diagnose", "foreshadow-check"]
    assert all(i["scope"] == "global" and i["editable"] is False for i in items)
    detail = (await client.get(f"{BASE}/{items[0]['id']}")).json()
    assert len(detail["body"]) > 600 and "续写" in detail["triggers"]
    sid = items[0]["id"]
    assert (await client.patch(f"{BASE}/{sid}", json={"name": "改名"})).status_code == 403
    assert (await client.delete(f"{BASE}/{sid}")).status_code == 403
    assert (await client.put(f"{BASE}/{sid}/references", json={"references": []})).status_code == 403
    assert (await client.post(f"{BASE}/{sid}/restore-builtin")).status_code == 403
    resp = await client.post(BASE, json=skill_body(scope="global"))
    assert resp.status_code == 403


async def test_crud_and_cross_user_404(client, make_client):
    await register(client)
    resp = await client.post(BASE, json=skill_body())
    assert resp.status_code == 201, resp.text
    skill = resp.json()
    assert skill["scope"] == "mine" and skill["editable"] is True
    assert skill["triggers"] == ["流程", "Flow"]
    assert [r["title"] for r in skill["references"]] == ["资料甲", "资料乙"]
    sid = skill["id"]
    resp = await client.patch(f"{BASE}/{sid}", json={"body": "新正文", "triggers": ["新词"]})
    assert resp.status_code == 200 and resp.json()["body"] == "新正文"
    resp = await client.put(f"{BASE}/{sid}/references", json={"references": [{"title": "只剩一篇", "content": "x"}]})
    assert [r["title"] for r in resp.json()["references"]] == ["只剩一篇"]
    assert (await client.post(BASE, json=skill_body())).status_code == 409

    bob = make_client()
    await register(bob, "bob")
    for method, path, body in [
        ("GET", f"{BASE}/{sid}", None),
        ("PATCH", f"{BASE}/{sid}", {"name": "x"}),
        ("DELETE", f"{BASE}/{sid}", None),
        ("PUT", f"{BASE}/{sid}/references", {"references": []}),
        ("POST", f"{BASE}/{sid}/copy", None),
        ("GET", f"{BASE}/{sid}/export", None),
        ("POST", f"{BASE}/{sid}/restore-builtin", None),
    ]:
        resp = await bob.request(method, path, json=body)
        assert resp.status_code == 404, (method, path, resp.text)
    assert all(i["id"] != sid for i in (await bob.get(BASE)).json())
    # bob 可以有同名标识（私有 Skill 按用户唯一）
    assert (await bob.post(BASE, json=skill_body())).status_code == 201
    assert (await client.delete(f"{BASE}/{sid}")).status_code == 204
    assert (await client.get(f"{BASE}/{sid}")).status_code == 404


async def test_admin_writes_global_and_restores_builtin(client, app, make_client):
    await register(client, "root")
    await make_admin(app, "root")
    resp = await client.post(BASE, json=skill_body(scope="global", slug="global-flow"))
    assert resp.status_code == 201 and resp.json()["scope"] == "global"
    gid = resp.json()["id"]
    # 全局 slug 部分唯一索引
    assert (await client.post(BASE, json=skill_body(scope="global", slug="global-flow"))).status_code == 409
    builtin = next(i for i in (await client.get(BASE, params={"scope": "global"})).json() if i["builtin_key"])
    resp = await client.patch(f"{BASE}/{builtin['id']}", json={"body": "管理员改过", "name": "改名"})
    assert resp.status_code == 200 and resp.json()["editable"] is True
    resp = await client.post(f"{BASE}/{builtin['id']}/restore-builtin")
    assert resp.status_code == 200 and resp.json()["name"] == builtin["name"] and resp.json()["body"] != "管理员改过"
    assert (await client.post(f"{BASE}/{gid}/restore-builtin")).status_code == 400

    alice = make_client()
    await register(alice)
    assert (await alice.get(f"{BASE}/{gid}")).status_code == 200
    assert (await alice.patch(f"{BASE}/{gid}", json={"name": "x"})).status_code == 403
    # 管理员看不到其他用户的私有 Skill
    mine = (await alice.post(BASE, json=skill_body(slug="alice-only"))).json()
    assert (await client.get(f"{BASE}/{mine['id']}")).status_code == 404
    assert (await client.patch(f"{BASE}/{mine['id']}", json={"name": "x"})).status_code == 404
    assert (await client.delete(f"{BASE}/{gid}")).status_code == 204


async def test_copy_global_appends_slug_suffix(client):
    await register(client)
    deslop = next(i for i in (await client.get(BASE)).json() if i["builtin_key"] == "deslop")
    first = (await client.post(f"{BASE}/{deslop['id']}/copy")).json()
    second = (await client.post(f"{BASE}/{deslop['id']}/copy")).json()
    third = (await client.post(f"{BASE}/{deslop['id']}/copy")).json()
    assert [first["slug"], second["slug"], third["slug"]] == ["deslop", "deslop-2", "deslop-3"]
    assert first["scope"] == "mine" and first["editable"] and first["source_skill_id"] == deslop["id"]
    assert (await client.patch(f"{BASE}/{first['id']}", json={"body": "我的版本"})).status_code == 200
    original = (await client.get(f"{BASE}/{deslop['id']}")).json()
    assert original["body"] != "我的版本"


async def test_skill_quota(client, monkeypatch):
    await register(client)
    monkeypatch.setattr(skill_service, "MAX_SKILLS_PER_OWNER", 2)
    assert (await client.post(BASE, json=skill_body(slug="a"))).status_code == 201
    assert (await client.post(BASE, json=skill_body(slug="b"))).status_code == 201
    resp = await client.post(BASE, json=skill_body(slug="c"))
    assert resp.status_code == 400 and "最多" in resp.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"slug": "Bad Slug"},
        {"slug": "-start"},
        {"triggers": ["有 空格"]},
        {"triggers": [f"t{i}" for i in range(11)]},
        {"references": [{"title": "../evil", "content": ""}]},
        {"references": [{"title": f"r{i}", "content": ""} for i in range(21)]},
    ],
)
async def test_create_validation(client, body):
    await register(client)
    assert (await client.post(BASE, json=skill_body(**body))).status_code == 422


async def _export(client, sid) -> tuple[str, bytes]:
    resp = await client.get(f"{BASE}/{sid}/export")
    assert resp.status_code == 200, resp.text
    return resp.headers["content-type"], resp.content


async def _import(client, name, data, scope="mine"):
    return await client.post(f"{BASE}/import", files={"file": (name, data)}, data={"scope": scope})


FIELDS = ("name", "description", "category", "triggers", "body")


async def test_export_import_roundtrip_markdown_and_zip(client):
    await register(client)
    plain = (await client.post(BASE, json=skill_body(slug="plain", references=[], body="正文\n\n---\n不是分隔"))).json()
    media, data = await _export(client, plain["id"])
    assert media.startswith("text/markdown") and data.startswith(b"---\n")
    await client.delete(f"{BASE}/{plain['id']}")
    resp = await _import(client, "SKILL.md", data)
    assert resp.status_code == 201, resp.text
    again = resp.json()
    assert again["slug"] == "plain" and all(again[k] == plain[k] for k in FIELDS)

    refs = [{"title": "Z 在前", "content": "第一篇\n"}, {"title": "A 在后", "content": "第二篇"}]
    full = (await client.post(BASE, json=skill_body(slug="full", references=refs))).json()
    media, data = await _export(client, full["id"])
    assert media == "application/zip"
    await client.delete(f"{BASE}/{full['id']}")
    again = (await _import(client, "full.zip", data)).json()
    assert all(again[k] == full[k] for k in FIELDS)
    assert [(r["title"], r["content"]) for r in again["references"]] == [(r["title"], r["content"]) for r in refs]

    # 没有说明 / 分类 / 触发词的 Skill 也能往返（导出写出 description: ''）
    bare = (
        await client.post(BASE, json=skill_body(slug="bare", description="", category="", triggers=[], references=[]))
    ).json()
    media, data = await _export(client, bare["id"])
    await client.delete(f"{BASE}/{bare['id']}")
    resp = await _import(client, "SKILL.md", data)
    assert resp.status_code == 201, resp.text
    assert all(resp.json()[k] == bare[k] for k in FIELDS)


def _zip(entries: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


SKILL_MD = "---\nname: zipped\ndescription: 压缩包导入\ntriggers: [zz]\n---\n正文"


async def test_zip_layout_root_and_single_dir_ignores_other_files(client):
    await register(client)
    data = _zip({"SKILL.md": SKILL_MD, "references/b.md": "B", "references/a.md": "A", "notes.txt": "忽略"})
    skill = (await _import(client, "x.zip", data)).json()
    assert [r["title"] for r in skill["references"]] == ["a", "b"]
    data = _zip({"pkg/SKILL.md": SKILL_MD.replace("zipped", "zipped-2"), "pkg/references/x.md": "X"})
    assert (await _import(client, "y.zip", data)).json()["reference_titles"] == ["x"]
    detail = (await client.get(BASE, params={"scope": "mine"})).json()
    assert {s["slug"] for s in detail} == {"zipped", "zipped-2"}


@pytest.mark.parametrize(
    "entries,needle",
    [
        ({"../SKILL.md": SKILL_MD}, "不安全"),
        ({"SKILL.md": SKILL_MD, "references/../../etc.md": "x"}, "不安全"),
        ({"/abs/SKILL.md": SKILL_MD}, "不安全"),
        ({"README.md": "x"}, "SKILL.md"),
        ({"a/b/SKILL.md": SKILL_MD}, "SKILL.md"),
        ({"SKILL.md": "没有 frontmatter"}, "frontmatter"),
        ({"SKILL.md": "---\nname: x\n---\n正文"}, "description"),
        ({"SKILL.md": "---\nname: x\ndescription: d\ntriggers: [[a, b]]\n---\n正文"}, "triggers"),
        ({"SKILL.md": "---\nname: x\ndescription: d\ncategory: {a: 1}\n---\n正文"}, "category"),
        ({"SKILL.md": SKILL_MD, **{f"references/r{i:02d}.md": "x" for i in range(21)}}, "20"),
    ],
)
async def test_zip_rejects_bad_packages(client, entries, needle):
    await register(client)
    resp = await _import(client, "bad.zip", _zip(entries))
    assert resp.status_code == 400, resp.text
    assert needle in resp.json()["detail"]


async def test_zip_bomb_and_upload_size(client):
    await register(client)
    bomb = _zip({"SKILL.md": SKILL_MD, "references/big.md": "a" * (6 * 1024 * 1024)})
    assert len(bomb) < skill_service.MAX_UPLOAD_BYTES
    resp = await _import(client, "bomb.zip", bomb)
    assert resp.status_code == 400 and "5MB" in resp.json()["detail"]
    resp = await _import(client, "big.md", b"x" * (skill_service.MAX_UPLOAD_BYTES + 10))
    assert resp.status_code == 400 and "2MB" in resp.json()["detail"]


async def test_import_global_requires_admin(client):
    await register(client)
    resp = await _import(client, "SKILL.md", SKILL_MD.encode(), scope="global")
    assert resp.status_code == 403


async def test_match_trigger_prefix_only_and_own_first(client, app):
    await register(client)
    await client.post(BASE, json=skill_body(slug="mine-continue", triggers=["续写"], references=[]))
    async with app.state.sessionmaker() as db:
        user_id = (await db.get(User, 1)).id
        own = await skill_service.match_trigger(db, user_id, "/续写 下一章")
        assert own is not None and own.slug == "mine-continue"
        assert (await skill_service.match_trigger(db, user_id, "/DESLOP")).builtin_key == "deslop"
        assert (await skill_service.match_trigger(db, user_id, "  /deslop\n这段")).builtin_key == "deslop"
        for text in ["请/续写", "续写", "/续写下一章", "说明 /deslop", "/不存在"]:
            assert await skill_service.match_trigger(db, user_id, text) is None, text


async def test_zip_bomb_rejected_before_decompressing(client, monkeypatch):
    """按声明大小先拦截：超限的包不解压任何条目（逐条读取时的字节计数是第二道防线）。"""
    await register(client)
    bomb = _zip({"SKILL.md": SKILL_MD, "references/big.md": "a" * (6 * 1024 * 1024)})

    def no_open(self, *args, **kwargs):
        raise AssertionError("超限的压缩包不应被解压")

    monkeypatch.setattr(zipfile.ZipFile, "open", no_open)
    resp = await _import(client, "bomb.zip", bomb)
    assert resp.status_code == 400 and "5MB" in resp.json()["detail"]


async def test_yaml_alias_bomb_in_frontmatter_rejected(client):
    """YAML 别名构造的指数级嵌套结构：不能被 str() 展开（否则一个几 KB 的文件就能耗尽内存）。"""
    await register(client)
    lines = ["---", "name: bomb", "description: d", 'a0: &a0 ["xxxxxxxxxx", "xxxxxxxxxx"]']
    for i in range(1, 30):
        lines.append(f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]")
    lines += ["triggers: *a29", "display_name: *a29", "---", "正文"]
    resp = await _import(client, "SKILL.md", "\n".join(lines).encode())
    assert resp.status_code == 400 and "triggers" in resp.json()["detail"]
