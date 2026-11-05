"""规划全部人工入口的作品隔离与只读能力边界。"""

from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role


async def test_every_planning_route_hides_other_projects_and_blocks_readonly_writes(client, system_admin, make_client):
    await register(client)
    project = await create_project(client)
    pid = project["id"]
    root = f"/api/v1/projects/{pid}"
    chapter = (await client.post(f"{root}/chapters", json={"title": "私有规划"})).json()
    source = {"kind": "chapter", "id": chapter["id"]}
    base = f"{root}/planning"
    reads = [
        ("/matrix", {}), ("/entries", {}), ("/tags", {}), ("/subplots", {}),
        ("/crossings", {}), ("/weave", {}), ("/detached", {}),
        ("/units/context-preview", {"source_kind": "chapter", "source_id": chapter["id"]}),
    ]
    writes = [
        ("PATCH", "/units/batch", {"expected_revision": 0, "targets": [source], "pov": {"op": "clear"}}),
        ("POST", "/units/merge", {"expected_revision": 0, "survivor_unit_id": 1,
                                  "other_unit_id": 2, "pov_character_id": None}),
        ("POST", "/units/1/binding", {"expected_revision": 0, "target": source}),
        ("POST", "/tags", {"expected_revision": 0, "name": "标签"}),
        ("PATCH", "/tags/1", {"expected_revision": 0, "name": "标签"}),
        ("DELETE", "/tags/1", None),
        ("POST", "/subplots", {"expected_revision": 0, "name": "支线"}),
        ("PATCH", "/subplots/1", {"expected_revision": 0, "name": "支线"}),
        ("DELETE", "/subplots/1", None),
        ("PUT", f"/subplots/1/nodes/chapter/{chapter['id']}", {"expected_revision": 0, "content": "目标"}),
        ("DELETE", f"/subplots/1/nodes/chapter/{chapter['id']}", None),
        ("POST", "/crossings", {"expected_revision": 0, "source": source, "title": "交汇",
                                "description": "共同推进", "subplot_ids": [1, 2]}),
        ("PATCH", "/crossings/1", {"expected_revision": 0, "title": "交汇"}),
        ("DELETE", "/crossings/1", None),
    ]
    other = make_client()
    actor = await register(other, "planning_reader")
    for path, params in reads:
        response = await other.get(f"{base}{path}", params=params)
        assert response.status_code == 404, (path, response.text)
        assert response.json()["detail"] == "项目不存在"
    for method, path, body in writes:
        response = await other.request(method, f"{base}{path}", json=body,
                                       params={"expected_revision": 0} if method == "DELETE" else None)
        assert response.status_code == 404, (method, path, response.text)
    role = await create_role(system_admin, "规划全入口只读", ["projects.read"])
    await assign_roles(system_admin, actor["id"], [role["id"]])
    for path, params in reads:
        response = await other.get(f"{base}{path}", params=params)
        assert response.status_code == 200, (path, response.text)
    for method, path, body in writes:
        response = await other.request(method, f"{base}{path}", json=body,
                                       params={"expected_revision": 0} if method == "DELETE" else None)
        assert response.status_code == 403, (method, path, response.text)
