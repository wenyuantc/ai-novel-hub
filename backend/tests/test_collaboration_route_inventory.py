"""Guard the collaboration audit against an accidentally empty route scan."""

from fastapi.routing import APIRoute


def _routes(routes, prefix=""):
    """FastAPI may defer included routers instead of flattening app.routes."""
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            yield from _routes(route.original_router.routes, prefix + route.include_context.prefix)
        elif isinstance(route, APIRoute):
            for method in route.methods:
                yield method, prefix + route.path


def test_route_inventory_covers_openapi_and_representative_sensitive_paths(app):
    scanned = set(_routes(app.routes))
    documented = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    assert len(documented) >= 469
    assert documented <= scanned
    assert {
        ("GET", "/api/v1/projects/{project_id}"),
        ("DELETE", "/api/v1/projects/{project_id}"),
        ("GET", "/api/v1/projects/{project_id}/backup"),
        ("PUT", "/api/v1/projects/{project_id}/reading-progress"),
        ("POST", "/api/v1/chapters/{chapter_id}/rewrite"),
        ("POST", "/api/v1/chapters/{chapter_id}/generate"),
        ("GET", "/api/v1/tasks/{task_id}/stream"),
        ("POST", "/api/v1/projects/{project_id}/kb/search"),
        ("POST", "/api/v1/projects/{project_id}/agent/changes/{change_id}/confirm"),
        ("GET", "/api/v1/series/{sid}/impact"),
    } <= documented
