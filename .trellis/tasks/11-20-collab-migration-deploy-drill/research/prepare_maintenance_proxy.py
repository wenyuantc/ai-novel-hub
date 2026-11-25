"""为独立复核夹具启动可启停的本地维护代理及旧版前端。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import fixture_backend
import fixture_supervisor
import h11
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


def unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def create_app(
    upstream: str, marker: Path, root: Path, backend: Path, upstream_pid: int, *, test_stage=None
) -> Starlette:
    if urlsplit(upstream).hostname != "127.0.0.1":
        raise ValueError("旧版后端不在本机回环地址")

    async def proxy(request):
        path = request.url.path
        allowed = (
            request.method in {"GET", "HEAD"}
            or path == "/api/v1/auth/login"
            or path.startswith("/api/v1/admin/")
            or (path.startswith("/api/v1/tasks/") and path.endswith(("/cancel", "/finish")))
        )
        if not allowed and await asyncio.to_thread(marker.exists):
            return JSONResponse({"detail": "C8 隔离环境维护中"}, status_code=503)
        sock = socket.socket()
        sock.setblocking(False)
        writer = None
        try:
            async with asyncio.timeout(30):
                port = urlsplit(upstream).port
                if port is None:
                    raise ValueError("upstream port missing")
                await asyncio.to_thread(fixture_backend.verify, root, backend, upstream, upstream_pid)
                if test_stage is not None:
                    await test_stage("before_connect")
                await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", port))
                client_host, client_port = sock.getsockname()
                if client_host != "127.0.0.1":
                    raise RuntimeError("fixture_client_not_loopback")
                # 先连接但不发送业务数据；确认该连接的对端由目标 PID 持有，再在同一 socket 上发请求。
                await asyncio.to_thread(
                    fixture_backend.verify, root, backend, upstream, upstream_pid, client_port=client_port
                )
                if test_stage is not None:
                    await test_stage("after_verification")
                reader, writer = await asyncio.open_connection(sock=sock)
                target = request.scope.get("raw_path", path.encode())
                query = request.scope.get("query_string", b"")
                if query:
                    target += b"?" + query
                excluded = {b"host", b"content-length", b"transfer-encoding", b"connection"}
                headers = [(key, value) for key, value in request.headers.raw if key.lower() not in excluded]
                headers.extend([(b"host", f"127.0.0.1:{port}".encode()), (b"connection", b"close")])
                body = await request.body()
                headers.append((b"content-length", str(len(body)).encode()))
                connection = h11.Connection(h11.CLIENT)
                writer.write(
                    connection.send(h11.Request(method=request.method.encode(), target=target, headers=headers))
                )
                if body:
                    writer.write(connection.send(h11.Data(data=body)))
                writer.write(connection.send(h11.EndOfMessage()))
                await writer.drain()
                chunks = []
                response_event = None
                while True:
                    event = connection.next_event()
                    if event is h11.NEED_DATA:
                        connection.receive_data(await reader.read(65536))
                    elif isinstance(event, h11.Response):
                        response_event = event
                    elif isinstance(event, h11.Data):
                        chunks.append(event.data)
                    elif isinstance(event, h11.EndOfMessage):
                        break
                    elif isinstance(event, h11.ConnectionClosed):
                        raise RuntimeError("fixture_backend_incomplete_response")
                if response_event is None:
                    raise RuntimeError("fixture_backend_response_missing")
                response = Response(b"".join(chunks), status_code=response_event.status_code)
                excluded_response = {b"content-length", b"transfer-encoding", b"connection"}
                response.raw_headers = [
                    (key, value) for key, value in response_event.headers if key.lower() not in excluded_response
                ]
                return response
        except (OSError, RuntimeError, ValueError, TimeoutError, h11.ProtocolError):
            return JSONResponse({"detail": "C8 夹具后端归属无法确认或连接失败"}, status_code=503)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            else:
                sock.close()

    return Starlette(routes=[Route("/{path:path}", proxy, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])])


def wait_url(url: str, process: subprocess.Popen) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(600):
        if process.poll() is not None:
            raise RuntimeError("夹具进程提前退出")
        try:
            with opener.open(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise TimeoutError("夹具进程未就绪")


def status(url: str, method: str = "GET") -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, method=method, data=b"{}" if method == "POST" else None)
    try:
        with opener.open(request, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def terminate_fixture_frontend(pid: int, frontend: Path) -> None:
    """历史 PID/cwd 不能证明跨进程交接身份，验证后仍拒绝组信号。"""
    try:
        group = os.getpgid(pid)
    except ProcessLookupError:
        raise RuntimeError("前端组长已退出，无法确认组内其他进程") from None
    if group != pid:
        raise RuntimeError("旧前端进程组身份不匹配")
    result = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        capture_output=True,
        text=True,
        check=False,
    )
    paths = [Path(line[1:]).resolve() for line in result.stdout.splitlines() if line.startswith("n")]
    if result.returncode != 0 or paths != [frontend]:
        raise RuntimeError("无法确认旧前端进程属于本夹具")
    raise RuntimeError("独立 PID 无认证守护，须使用本夹具监督进程的控制通道")


def remove_owned_marker(marker: Path, identity: tuple[int, int]) -> None:
    """只清理本次验证创建且未被替换的标记。"""
    try:
        current = marker.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity:
        marker.unlink()


def install(metadata: Path, *, preserve_maintenance: bool = False) -> None:
    metadata = metadata.resolve(strict=True)
    root = metadata.parent
    if not root.name.startswith("ainovel-c8-drill-") or metadata.name != "fixture.json":
        raise ValueError("不是 C8 独立夹具")
    info = json.loads(metadata.read_text(encoding="utf-8"))
    if Path(info["root"]).resolve(strict=True) != root or info["shape"] != "sqlite":
        raise ValueError("夹具目录或数据库类型不匹配")
    if urlsplit(info["old_app_url"]).hostname != "127.0.0.1":
        raise ValueError("旧版后端不在本机回环地址")
    fixture_backend.verify(root, root / "old/backend", info["old_app_url"], info["old_app_pid"])
    frontend = Path(info["old_frontend_path"]).resolve(strict=True)
    if frontend != root / "old/frontend":
        raise ValueError("前端目录不属于本次夹具")
    if info.get("old_frontend_pid") or info.get("maintenance_proxy_pid"):
        raise RuntimeError("夹具已有前端或代理，须由原监督进程清理")
    marker = root / "maintenance.enabled"
    if marker.exists() and not preserve_maintenance:
        raise FileExistsError("维护标记已存在")
    if preserve_maintenance and (not marker.is_file() or marker.is_symlink()):
        raise RuntimeError("重定向前须保持本夹具维护标记开启")
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    env.update(LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8", NO_PROXY="*")
    proxy_port, frontend_port = unused_port(), unused_port()
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    frontend_url = f"http://127.0.0.1:{frontend_port}"
    proxy_env = dict(
        env,
        C8_UPSTREAM=info["old_app_url"],
        C8_MARKER=str(marker),
        C8_PROXY_PORT=str(proxy_port),
        C8_FIXTURE_ROOT=str(root),
        C8_UPSTREAM_CWD=str(root / "old/backend"),
        C8_UPSTREAM_PID=str(info["old_app_pid"]),
    )
    control_path = None
    marker_identity = None
    try:
        front_env = dict(env, VITE_BACKEND_URL=proxy_url)
        state, control_path = fixture_supervisor.start(
            root,
            "old-" + secrets.token_hex(6),
            [
                {
                    "args": [sys.executable, str(Path(__file__).resolve()), "--serve"],
                    "cwd": str(root),
                    "env": proxy_env,
                    "log": "maintenance-proxy.log",
                    "ready": [proxy_url + "/api/v1/auth/config"],
                },
                {
                    "args": [
                        "npm",
                        "run",
                        "dev",
                        "--",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(frontend_port),
                        "--strictPort",
                    ],
                    "cwd": str(frontend),
                    "env": front_env,
                    "log": "maintenance-frontend.log",
                    "ready": [frontend_url + "/", frontend_url + "/api/v1/auth/config"],
                },
            ],
        )
        proxy_pid, front_pid = state["children"]
        # 不登录、不提交业务数据，分别确认普通写入口被拦截和管理入口已转发。
        if not preserve_maintenance:
            marker.touch(exist_ok=False)
            marker_info = marker.lstat()
            marker_identity = (marker_info.st_dev, marker_info.st_ino)
        assert status(proxy_url + "/api/v1/projects", "POST") == 503
        assert status(proxy_url + "/api/v1/admin/backups/settings") == 401
        if marker_identity is not None:
            remove_owned_marker(marker, marker_identity)
            if marker.exists():
                raise RuntimeError("维护标记已被其他操作替换")
            marker_identity = None
            assert status(proxy_url + "/api/v1/projects", "POST") == 401
        info.update(
            old_frontend_url=frontend_url,
            old_frontend_pid=front_pid,
            maintenance_proxy_url=proxy_url,
            maintenance_proxy_pid=proxy_pid,
            maintenance_marker=str(marker),
            old_supervisor_control=str(control_path),
        )
        metadata.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(metadata, 0o600)
        print(
            json.dumps(
                {
                    "frontend_url": frontend_url,
                    "frontend_pid": front_pid,
                    "proxy_url": proxy_url,
                    "proxy_pid": proxy_pid,
                    "marker": str(marker),
                    "blocked_post": 503,
                    "forwarded_admin_unauthenticated": 401,
                    "disabled_post_unauthenticated": None if preserve_maintenance else 401,
                    "maintenance_preserved": preserve_maintenance,
                },
                ensure_ascii=False,
            )
        )
    except BaseException:
        if marker_identity is not None:
            remove_owned_marker(marker, marker_identity)
        if control_path is not None:
            fixture_supervisor.stop(control_path)
        raise


def retarget(metadata: Path, upstream: str, upstream_pid: int) -> None:
    metadata = metadata.resolve(strict=True)
    root = metadata.parent
    if not root.name.startswith("ainovel-c8-drill-") or metadata.name != "fixture.json":
        raise ValueError("不是 C8 独立夹具")
    info = json.loads(metadata.read_text(encoding="utf-8"))
    if Path(info["root"]).resolve(strict=True) != root or info["shape"] != "sqlite":
        raise ValueError("夹具目录或数据库类型不匹配")
    marker = root / "maintenance.enabled"
    if Path(info["maintenance_marker"]) != marker or not marker.is_file() or marker.is_symlink():
        raise RuntimeError("重定向前须保持本夹具维护标记开启")
    fixture_backend.verify(root, root / "old/backend", upstream, upstream_pid)
    control = Path(info["old_supervisor_control"]).resolve(strict=True)
    if (
        control.parent != root
        or not control.name.startswith("old-")
        or not control.name.endswith("-supervisor-state.json")
    ):
        raise RuntimeError("旧监督控制文件不属于本夹具")
    fixture_supervisor.stop(control)
    for field in ("old_frontend_pid", "maintenance_proxy_pid", "old_supervisor_control"):
        info.pop(field, None)
    info["old_app_url"] = upstream.rstrip("/")
    info["old_app_pid"] = upstream_pid
    metadata.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(metadata, 0o600)
    install(metadata, preserve_maintenance=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path, nargs="?")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--retarget", metavar="UPSTREAM")
    parser.add_argument("--backend-pid", type=int)
    args = parser.parse_args()
    if args.serve:
        uvicorn.run(
            create_app(
                os.environ["C8_UPSTREAM"],
                Path(os.environ["C8_MARKER"]),
                Path(os.environ["C8_FIXTURE_ROOT"]),
                Path(os.environ["C8_UPSTREAM_CWD"]),
                int(os.environ["C8_UPSTREAM_PID"]),
            ),
            host="127.0.0.1",
            port=int(os.environ["C8_PROXY_PORT"]),
            log_level="warning",
            access_log=False,
        )
    elif args.metadata is not None and args.retarget and args.backend_pid is not None:
        retarget(args.metadata, args.retarget, args.backend_pid)
    elif args.metadata is not None and not args.retarget and args.backend_pid is None:
        install(args.metadata)
    else:
        parser.error("请提供夹具 metadata；重定向时同时提供 --retarget 和 --backend-pid")
