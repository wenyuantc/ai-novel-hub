"""从 Git 归档和指定 C7 文件构建应用镜像，在隔离容器中验证运行。"""

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
DATA_SUFFIX = re.compile(r"(?i)(\.db3?|\.sqlite3?|\.dump|\.backup|\.bak|\.sql|\.tar|\.tgz|\.zip|\.7z|\.log)($|[.-])")


def command(args, *, env, cwd, timeout=180, check=True):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"test command failed: {result.returncode}")
    return result


def controlled_source(context, archive):
    with tarfile.open(archive) as source:
        for member in source:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise RuntimeError("unsupported archive entry")
            if path.name.startswith(".env") and path.name != ".env.example":
                continue
            if (path.parts[:2] == ("backend", "backups") or path.name.endswith(".leases.lock")
                    or DATA_SUFFIX.search(path.name)):
                continue
            destination = context.joinpath(*path.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as data, destination.open("wb") as output:
                    shutil.copyfileobj(data, output)
                destination.chmod(member.mode & 0o777)
            else:
                raise RuntimeError("unsupported archive entry")
    for name in ("Dockerfile", ".dockerignore", "backend/alembic/env.py", "backend/docker-entrypoint.sh",
                 "backend/app/services/backups/sqlite_restore.py"):
        destination = context / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)


def main():
    env = {key: os.environ[key] for key in PASSTHROUGH if key in os.environ}
    run_id = secrets.token_hex(8)
    tag = f"ainovel-c7-app-context-{run_id}"
    label = f"ainovel.c7.app-context={run_id}"
    name = f"c7appcontext{run_id}"
    tag_filter = f"reference={tag}"
    label_filter = f"label={label}"
    for args in (["docker", "image", "ls", "-q", "--filter", tag_filter],
                 ["docker", "ps", "-aq", "--filter", label_filter],
                 ["docker", "ps", "-aq", "--filter", f"name=^/{name}$"]):
        if command(args, env=env, cwd=ROOT).stdout.strip():
            raise RuntimeError("test resource name already exists")

    attempted = False
    uncertain = False
    try:
        with tempfile.TemporaryDirectory(prefix="c7-application-context-") as temporary:
            directory = Path(temporary)
            archive = directory / "source.tar"
            context = directory / "context"
            context.mkdir()
            command(["git", "archive", "--format=tar", f"--output={archive}", "HEAD", "Dockerfile", ".dockerignore",
                     "backend", "frontend"], env=env, cwd=ROOT)
            controlled_source(context, archive)
            ignore_sha = hashlib.sha256((context / ".dockerignore").read_bytes()).hexdigest()
            checked_sources = ("backend/alembic/env.py", "backend/app/services/backups/sqlite_restore.py")
            source_hashes = {
                name: hashlib.sha256((context / name).read_bytes()).hexdigest() for name in checked_sources
            }
            envfile = directory / "runtime.env"
            envfile.write_text(
                "DATABASE_URL=sqlite+aiosqlite:////tmp/c7-application.db\n"
                f"JWT_SECRET={secrets.token_hex(32)}\n"
                f"ENCRYPTION_KEY={base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}\n"
                "BACKUP_DIR=/tmp/c7-backups\nUVICORN_WORKERS=1\n"
            )
            envfile.chmod(0o600)
            attempted = True
            try:
                build = command(["docker", "build", "--progress", "plain", "--label", label,
                                 "--tag", tag, "."], env=env, cwd=context, timeout=900)
                if " [internal] load build definition" not in build.stdout + build.stderr:
                    raise RuntimeError("default build did not report BuildKit")
                started = command(["docker", "run", "-d", "--pull=never", "--name", name, "--label", label,
                                   "--network", "none", "--tmpfs", "/tmp:rw,size=256m", "--env-file", str(envfile),
                                   tag], env=env, cwd=ROOT, timeout=120)
                container = started.stdout.strip()
            except BaseException:
                uncertain = True
                raise

            inspect = command(["docker", "inspect", "--format",
                               "{{json .HostConfig.NetworkMode}}|{{json .HostConfig.Tmpfs}}|{{json .Mounts}}",
                               container], env=env, cwd=ROOT).stdout.strip().split("|", 2)
            network, tmpfs, mounts = (json.loads(value) for value in inspect)
            if network != "none" or "/tmp" not in (tmpfs or {}) or any(
                mount.get("Type") == "volume" for mount in mounts
            ):
                raise RuntimeError("isolated application storage or network differs")

            probe = (
                "import json,urllib.request\n"
                "result={}\n"
                "for path in ('/healthz','/api/v1/auth/config','/'):\n"
                " with urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=2) as response:\n"
                "  body=json.loads(response.read(4097)) if path.endswith('/config') else {}\n"
                "  result[path]=(response.status==200 and (not path.endswith('/config') or "
                "(body.get('registration_enabled') is True and body.get('mcp_enabled') is True)))\n"
                "print(json.dumps(result))\n"
            )
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                result = command(["docker", "exec", container, "python", "-c", probe],
                                 env=env, cwd=ROOT, timeout=10, check=False)
                if result.returncode == 0:
                    value = json.loads(result.stdout)
                    if value == {"/healthz": True, "/api/v1/auth/config": True, "/": True}:
                        break
                status = command(["docker", "inspect", "--format", "{{.State.Running}}", container],
                                 env=env, cwd=ROOT).stdout.strip()
                if status != "true":
                    raise RuntimeError("isolated application exited before readiness")
                time.sleep(0.5)
            else:
                raise RuntimeError("isolated application was not ready")
            for source, expected in source_hashes.items():
                path = f"/app/{source}"
                hashed = command(["docker", "exec", container, "sha256sum", path], env=env, cwd=ROOT).stdout.split()
                if hashed[:1] != [expected] or hashlib.sha256((ROOT / source).read_bytes()).hexdigest() != expected:
                    raise RuntimeError("application source differs")
                print(f"镜像内 {source} SHA-256：{expected}")
            if ignore_sha != hashlib.sha256((ROOT / ".dockerignore").read_bytes()).hexdigest():
                raise RuntimeError("dockerignore changed during test")
            print(f"构建上下文 .dockerignore SHA-256：{ignore_sha}")
            print("最终 .dockerignore 的实际应用镜像构建成功，隔离启动和健康/数据库设置/前端请求均通过")
    finally:
        if attempted:
            try:
                for container in command(["docker", "ps", "-aq", "--filter", label_filter],
                                         env=env, cwd=ROOT).stdout.splitlines():
                    command(["docker", "rm", "-f", "-v", container], env=env, cwd=ROOT)
                if command(["docker", "image", "ls", "-q", "--filter", tag_filter],
                           env=env, cwd=ROOT).stdout.strip():
                    command(["docker", "image", "rm", tag], env=env, cwd=ROOT)
                if command(["docker", "ps", "-aq", "--filter", label_filter], env=env, cwd=ROOT).stdout.strip() or (
                    command(["docker", "image", "ls", "-q", "--filter", tag_filter],
                            env=env, cwd=ROOT).stdout.strip()
                ):
                    uncertain = True
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                uncertain = True
            if uncertain:
                print(f"测试资源清理未确认，请核对本轮镜像标签 {tag} 和资源标签 {label}", file=sys.stderr)
                raise RuntimeError("cleanup unconfirmed") from None


if __name__ == "__main__":
    main()
