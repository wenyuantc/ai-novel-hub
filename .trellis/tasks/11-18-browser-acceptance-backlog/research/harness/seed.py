"""经 HTTP 接口建立验收账号与夹具，可重复执行（已存在则复用）。结果写入 <dir>/fixtures.json。

用法：backend/.venv/bin/python seed.py --dir <serve.py 的临时目录>

口令 QA_PASSWORD 是只在隔离库里有效的固定测试口令；超级管理员口令由 serve.py 现场生成并只存于 state.json。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

QA_PASSWORD = "Qa-pass-2026"
USERS = ["qa_author", "qa_editor", "qa_viewer", "qa_series", "qa_series_editor", "qa_admin_rw", "qa_admin_ro"]
MAIN_TITLE = "剑影长歌"
BOOK_B_TITLE = "剑影续篇"
SERIES_NAME = "剑影系列"
ROLE_RW = "QA 作品读写"
ROLE_RO = "QA 作品只读"
SKILL_SLUG = "qa-tone"
SKILL_TRIGGER = "基调"

FILLER = "林远沿着官道继续赶路，天色平常，一路无事。路边的茶棚冒着热气，挑担的货郎吆喝着走过。"


def chapter_texts() -> dict[int, str]:
    texts = {i: f"第{i}章。{FILLER}" for i in range(1, 16)}
    texts[5] = "山谷一战，林远力竭。林远的佩剑在山谷中被巨石砸断，彻底毁了。他只得赤手下山。"
    texts[12] = "敌骑逼近城门。林远拔出佩剑迎敌，剑光如雪。守军士气大振。"
    return texts


class Api:
    def __init__(self, origin: str, username: str, password: str):
        self.username = username
        self.c = httpx.Client(base_url=f"{origin}/api/v1", timeout=60)
        self.password = password

    def call(self, method: str, path: str, ok=(200, 201, 202, 204), **kw):
        r = self.c.request(method, path, **kw)
        if r.status_code not in ok:
            raise RuntimeError(f"[{self.username}] {method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else None

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.call("POST", path, json=json, **kw)

    def patch(self, path, json=None, **kw):
        return self.call("PATCH", path, json=json, **kw)

    def put(self, path, json=None, **kw):
        return self.call("PUT", path, json=json, **kw)


def login(origin: str, username: str, password: str, register: bool) -> Api:
    api = Api(origin, username, password)
    r = api.c.post("/auth/login", json={"username": username, "password": password})
    if r.status_code != 200 and register:
        api.call("POST", "/auth/register", json={"username": username, "password": password})
        r = api.c.post("/auth/login", json={"username": username, "password": password})
    if r.status_code != 200:
        raise RuntimeError(f"login {username}: {r.status_code} {r.text[:200]}")
    return api


def items(payload):
    if isinstance(payload, dict):
        for key in ("items", "data", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return payload or []


def wait_task(api: Api, task: dict, within: float = 120) -> dict:
    deadline = time.time() + within
    while True:
        cur = api.get(f"/tasks/{task['id']}", params={"task_key": task["task_key"]})
        if cur["status"] in ("succeeded", "failed", "cancelled", "interrupted"):
            return cur
        if time.time() > deadline:
            raise RuntimeError(f"任务 {task['id']} 超时：{cur}")
        time.sleep(0.5)


def ensure_ai_config(api: Api, base_url: str) -> int:
    for cfg in items(api.get("/ai-configs")):
        if cfg.get("name") == "本地假模型":
            if not cfg.get("is_default"):
                api.post(f"/ai-configs/{cfg['id']}/default")
            return cfg["id"]
    cfg = api.post("/ai-configs", json={"name": "本地假模型", "provider": "openai_compat", "base_url": base_url,
                                        "api_key": "local-fake", "model": "fake-model", "is_default": True})
    return cfg["id"]


def ensure_project(api: Api, title: str, **kw) -> dict:
    for p in items(api.get("/projects", params={"page_size": 100})):
        if p["title"] == title:
            return p
    return api.post("/projects", json={"title": title, "genre": "武侠", "premise": "少年剑客林远的江湖路", **kw})


def ensure_chapters(api: Api, pid: int, texts: dict[int, str]) -> dict[int, int]:
    existing = {c["chapter_no"]: c["id"] for c in items(api.get(f"/projects/{pid}/chapters", params={"page_size": 200}))}
    for no, text in texts.items():
        if no not in existing:
            existing[no] = api.post(f"/projects/{pid}/chapters",
                                    json={"title": f"第{no}回", "chapter_no": no, "content": text})["id"]
    return existing


def ensure_character(api: Api, pid: int, name: str, **kw) -> int:
    for ch in items(api.get(f"/projects/{pid}/characters", params={"page_size": 200})):
        if ch["name"] == name:
            return ch["id"]
    return api.post(f"/projects/{pid}/characters", json={"name": name, **kw})["id"]


def accept_all(api: Api) -> list[dict]:
    accepted = []
    for inv in items(api.get("/collaboration/invitations")):
        scope = inv.get("scope")
        inv_id = inv.get("id") or inv.get("invitation_id")
        revision = inv.get("revision") or inv.get("expected_revision")
        api.post(f"/collaboration/invitations/{scope}/{inv_id}/accept", json={"expected_revision": revision})
        accepted.append(inv)
    return accepted


def invite_project(owner: Api, pid: int, username: str, role: str, allow_owner_ai: bool = False) -> None:
    members = items(owner.get(f"/projects/{pid}/collaborators"))
    if any((m.get("username") or (m.get("user") or {}).get("username")) == username for m in members):
        return
    owner.post(f"/projects/{pid}/collaborators", json={"username": username, "role": role,
                                                       "allow_owner_ai": allow_owner_ai})


def invite_series(owner: Api, sid: int, username: str) -> None:
    members = items(owner.get(f"/series/{sid}/collaborators"))
    if any((m.get("username") or (m.get("user") or {}).get("username")) == username for m in members):
        return
    owner.post(f"/series/{sid}/collaborators", json={"username": username})


def ensure_role(admin: Api, name: str, permissions: list[str]) -> int:
    for role in items(admin.get("/admin/roles")):
        if role["name"] == name:
            return role["id"]
    return admin.post("/admin/roles", json={"name": name, "description": "浏览器验收专用", "permissions": permissions})["id"]


def assign_role(admin: Api, username: str, role_id: int) -> int:
    users = items(admin.get("/admin/users", params={"q": username, "keyword": username, "page_size": 100}))
    user = next(u for u in users if u["username"] == username)
    detail = admin.get(f"/admin/users/{user['id']}")
    current = [r["id"] for r in detail.get("roles", []) if isinstance(r, dict)]
    if role_id not in current:
        admin.put(f"/admin/users/{user['id']}/roles",
                  json={"authorization_revision": detail.get("authorization_revision", 0), "role_ids": [role_id]})
    return user["id"]


def analyze_chapter(api: Api, cid: int) -> dict:
    task = api.post(f"/chapters/{cid}/analyze", json={})
    done = wait_task(api, task)
    if done["status"] != "succeeded":
        raise RuntimeError(f"分析第 {cid} 章失败：{done}")
    return done


def seed(root: Path) -> dict:
    state = json.loads((root / "state.json").read_text())
    origin = state["origin"]
    admin = login(origin, state["admin_username"], state["admin_password"], register=False)
    apis = {name: login(origin, name, QA_PASSWORD, register=True) for name in USERS}
    author = apis["qa_author"]
    fx: dict = {"origin": origin, "password_note": "测试账号口令见 seed.py 的 QA_PASSWORD（仅隔离库有效）",
                "users": {}}
    for name, api in apis.items():
        fx["users"][name] = api.get("/auth/me")["id"]

    # 管理角色
    rw = ensure_role(admin, ROLE_RW, ["projects.read", "projects.edit"])
    ro = ensure_role(admin, ROLE_RO, ["projects.read"])
    assign_role(admin, "qa_admin_rw", rw)
    assign_role(admin, "qa_admin_ro", ro)
    fx["roles"] = {"projects_rw": rw, "projects_ro": ro}

    # 模型配置：作者本人默认文本模型（编辑者默认无本人模型，靠作者授权）
    fx["ai_configs"] = {"qa_author": ensure_ai_config(author, state["model_base_url"])}

    # 主作品
    main = ensure_project(author, MAIN_TITLE)
    pid = main["id"]
    chars = {
        "林远": ensure_character(author, pid, "林远", role_type="主角", profile="少年剑客，师承山中剑庐"),
        "苏晴": ensure_character(author, pid, "苏晴", role_type="配角", profile="随行医者"),
        "老周": ensure_character(author, pid, "老周", role_type="配角", profile="渡口船夫"),
    }
    chapters = ensure_chapters(author, pid, chapter_texts())

    # 第 5 章分析：产生剧情记忆与设定变化（确认待定回写）
    analyses = items(author.get(f"/chapters/{chapters[5]}/analyses"))
    if not analyses:
        analyze_chapter(author, chapters[5])
        analyses = items(author.get(f"/chapters/{chapters[5]}/analyses"))
    latest = author.get(f"/analyses/{analyses[0]['id']}")
    for change in latest.get("changes", []):
        if change.get("status") == "pending":
            author.post(f"/analysis-changes/{change['id']}/confirm", json={})
    latest = author.get(f"/analyses/{analyses[0]['id']}")
    fx["analysis_ch5"] = {"id": latest["id"],
                          "changes": [{"id": c["id"], "status": c.get("status"), "type": c.get("change_type")
                                       or c.get("type")} for c in latest.get("changes", [])]}
    fx["memories_main"] = len(items(author.get(f"/projects/{pid}/memories", params={"page_size": 100})))

    # 多场景 + 分区块章节（第 3 章）
    ch3 = f"/projects/{pid}/chapters/{chapters[3]}"
    scenes = items(author.get(f"{ch3}/scenes"))
    if len(scenes) < 3:
        page = author.get(f"{ch3}/scenes")
        rev = page["content_revision"]
        for title, text in (("茶棚", "林远在茶棚歇脚，听老板讲起南边的传闻。"),
                            ("夜路", "夜色渐深，林远提着灯笼走在山道上，远处有狼嚎。")):
            author.post(f"{ch3}/scenes", json={"expected_content_revision": rev, "title": title, "content": text})
            rev = author.get(f"{ch3}/scenes")["content_revision"]
        scenes = items(author.get(f"{ch3}/scenes"))
    first_scene = scenes[0]["id"]
    doc_url = f"{ch3}/scenes/{first_scene}/document"
    doc = author.get(doc_url)
    if not doc["document"].get("sections"):
        key = "5b1f7c3e-9a42-4d0e-8c11-3a7d2f6b9e01"
        body = {"expected_content_revision": doc["content_revision"], "document": doc["document"],
                "add_section_keys": [key], "delete_section_keys": [], "add_marker_keys": [],
                "delete_marker_keys": [], "mapped_section_keys": [], "mapped_marker_keys": []}
        body["document"]["sections"] = [{
            "key": key, "anchor": min(4, len(doc["document"]["content"])), "order": 0, "title": "备选开头",
            "content": "林远推开柴门，晨雾扑面而来。他回头望了一眼剑庐，转身下山。",
            "formatting": {"version": 1, "boundaries": [], "inline": [], "blocks": []}, "include_in_ai": True}]
        author.put(doc_url, json=body)
        doc = author.get(doc_url)
    fx["scene_chapter"] = {"chapter_id": chapters[3], "scene_ids": [s["id"] for s in items(author.get(f"{ch3}/scenes"))],
                           "section_scene_id": first_scene,
                           "section_keys": [s["key"] for s in doc["document"].get("sections", [])]}

    # 笔记
    notes = {n["title"]: n for n in items(author.get(f"/projects/{pid}/notes", params={"page_size": 100}))}
    plan = [("江湖地图草稿", True, True, "南下路线：青云山 → 渡口 → 临江城。"),
            ("佩剑来历", True, False, "佩剑为师父所赠，第 5 章被毁。"),
            ("杂记", False, False, "待整理的零散想法。")]
    fx["notes"] = {}
    for title, fav, pin, content in plan:
        note = notes.get(title) or author.post(f"/projects/{pid}/notes", json={"title": title, "content": content})
        author.put(f"/projects/{pid}/notes/{note['id']}/flags", json={"is_favorite": fav, "is_pinned": pin})
        fx["notes"][title] = note["id"]

    # Skill：下拉带默认值 + 必填单行 + 触发词
    skills = [s for s in items(author.get("/skills")) if s.get("slug") == SKILL_SLUG]
    skill = skills[0] if skills else author.post("/skills", json={
        "name": "基调与重点人物", "slug": SKILL_SLUG, "description": "按指定基调写作，突出重点人物",
        "category": "写作", "triggers": [SKILL_TRIGGER],
        "body": "请以{{ tone }}的基调写作，并重点刻画{{ focus }}的言行与心理。",
        "inputs": [{"name": "tone", "label": "基调", "kind": "select", "options": ["轻松", "沉重"], "default": "轻松"},
                   {"name": "focus", "label": "重点人物", "kind": "text", "required": True}]})
    fx["skill"] = {"id": skill["id"], "slug": SKILL_SLUG, "trigger": SKILL_TRIGGER}

    # 自定义分类（按标签匹配）
    tag_plan = {"林远": ["主角", "剑客"], "苏晴": ["医者"], "老周": ["江湖人"]}
    cats_payload = author.get(f"/projects/{pid}/setting-categories")
    cat_rev = cats_payload.get("categories_revision", cats_payload.get("revision", 0))
    for name, tags in tag_plan.items():
        current = author.get(f"/projects/{pid}/setting-entries/character/{chars[name]}/tags")
        if sorted(current.get("setting_tags", [])) != sorted(tags):
            author.patch(f"/projects/{pid}/setting-entries/character/{chars[name]}/tags",
                         json={"setting_tags": tags, "expected_categories_revision": cat_rev})
            cats_payload = author.get(f"/projects/{pid}/setting-categories")
            cat_rev = cats_payload.get("categories_revision", cats_payload.get("revision", 0))
    existing_cats = {c["name"]: c["id"] for c in items(cats_payload)}
    fx["categories"] = {}
    for name, rules, mode in (("主角阵营", [{"kind": "tag", "tag": "主角"}], "any"),
                              ("江湖人物", [{"kind": "tag", "tag": "剑客"}, {"kind": "tag", "tag": "江湖人"}], "any"),
                              ("医者", [{"kind": "tag", "tag": "医者"}], "all")):
        if name in existing_cats:
            fx["categories"][name] = existing_cats[name]
            continue
        cats_payload = author.get(f"/projects/{pid}/setting-categories")
        created = author.post(f"/projects/{pid}/setting-categories", json={
            "name": name, "applicable_types": ["character"], "match_mode": mode, "rules": rules,
            "expected_categories_revision": cats_payload.get("categories_revision", 0),
            "expected_fields_revision": cats_payload.get("fields_revision", 0)})
        fx["categories"][name] = (created.get("item") or created)["id"]

    # 系列：主作品 + 续篇，共享条目已采用 / 未采用各一
    book_b = ensure_project(author, BOOK_B_TITLE)
    ensure_chapters(author, book_b["id"], {1: "续篇第一章。林远抵达临江城，城中正值灯会。"})
    series = next((s for s in items(author.get("/series")) if s["name"] == SERIES_NAME), None)
    if series is None:
        series = author.post("/series", json={"name": SERIES_NAME})
    sid = series["id"]
    detail = author.get(f"/series/{sid}")
    book_ids = {b.get("project_id") or b.get("id") for b in detail.get("books", [])}
    for project_id in (pid, book_b["id"]):
        if project_id not in book_ids:
            author.post(f"/series/{sid}/books", json={"expected_revision": author.get(f"/series/{sid}")["revision"],
                                                      "project_id": project_id})
    entries = {(e.get("data") or {}).get("name") or (e.get("data") or {}).get("title") or e.get("name")
               or e.get("title"): e for e in items(author.get(f"/series/{sid}/entries"))}
    if "沈青" not in entries:
        res = author.post(f"/series/{sid}/entries", json={
            "expected_revision": author.get(f"/series/{sid}")["revision"], "kind": "character",
            "data": {"name": "沈青", "profile": "林远的同门师姐，两部作品共用的角色", "gender": "女"}})
        entries["沈青"] = res.get("item") or res
    if "临江城" not in entries:
        res = author.post(f"/series/{sid}/entries", json={
            "expected_revision": author.get(f"/series/{sid}")["revision"], "kind": "world",
            "data": {"title": "临江城", "category": "place", "content": "江畔大城，尚未被任何作品采用。"}})
        entries["临江城"] = res.get("item") or res
    adopted = entries["沈青"]
    entry_detail = author.get(f"/series/{sid}/entries/{adopted['id']}")
    used = {u.get("project_id") for u in (entry_detail.get("uses") or [])}
    if pid not in used:
        author.post(f"/series/{sid}/entries/{adopted['id']}/uses",
                    json={"expected_revision": author.get(f"/series/{sid}")["revision"], "project_id": pid})
    fx["series"] = {"id": sid, "books": [pid, book_b["id"]],
                    "entry_adopted": {"id": adopted["id"], "name": "沈青", "used_by": [pid]},
                    "entry_unadopted": {"id": entries["临江城"]["id"], "name": "临江城"}}

    # 协作邀请与接受
    invite_project(author, pid, "qa_editor", "editor", allow_owner_ai=True)
    invite_project(author, pid, "qa_viewer", "viewer")
    invite_project(author, pid, "qa_admin_rw", "viewer")
    invite_project(author, pid, "qa_admin_ro", "viewer")
    invite_project(author, book_b["id"], "qa_series_editor", "editor")
    invite_series(author, sid, "qa_series")
    invite_series(author, sid, "qa_series_editor")
    for name in ("qa_editor", "qa_viewer", "qa_admin_rw", "qa_admin_ro", "qa_series", "qa_series_editor"):
        accept_all(apis[name])
    fx["collaborators"] = {
        "project": {str(pid): {"qa_editor": "editor(allow_owner_ai)", "qa_viewer": "viewer",
                               "qa_admin_rw": "viewer", "qa_admin_ro": "viewer"},
                    str(book_b["id"]): {"qa_series_editor": "editor"}},
        "series": {str(sid): ["qa_series", "qa_series_editor"]},
    }
    fx["main_project"] = {"id": pid, "title": MAIN_TITLE, "chapters": {str(k): v for k, v in sorted(chapters.items())},
                          "characters": chars}
    fx["book_b"] = {"id": book_b["id"], "title": BOOK_B_TITLE}
    return fx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True)
    root = Path(parser.parse_args().dir).resolve()
    fx = seed(root)
    (root / "fixtures.json").write_text(json.dumps(fx, ensure_ascii=False, indent=2))
    print(json.dumps(fx, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        sys.exit(str(exc))
