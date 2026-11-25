"""以全是假数据的独立目录和独占镜像验证 .dockerignore。"""

import argparse
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
BLOCKED = {
    ".env", ".env.local", ".env.production", ".envrc", "backend/.env", "backend/.env.local",
    "backend/nested/.env.production", "backend/dev.db", "backend/dev.db-wal", "backend/dev.db-shm",
    "backend/dev.db-journal", "backend/dev.db.leases.sqlite3", "backend/dev.db.leases.sqlite3-wal",
    "backend/dev.db.leases.sqlite3-shm", "backend/dev.db.leases.lock", "backend/nested/site.DB",
    "backend/nested/site.sqlite", "backend/nested/site.sqlite-wal", "backend/nested/site.sqlite3",
    "backend/nested/site.sqlite3-journal", "backend/nested/site.db3", "backend/nested/site.db3-shm",
    "backend/nested/snapshot.dump", "backend/nested/snapshot.dump.gpg", "backend/nested/snapshot.backup",
    "backend/nested/snapshot.backup.gz", "backend/nested/snapshot.bak", "backend/nested/snapshot.sql",
    "backend/nested/snapshot.sql.gz", "backend/nested/snapshot.SQL", "backend/nested/snapshot.tar",
    "backend/nested/snapshot.tar.gz", "backend/nested/snapshot.tgz", "backend/nested/snapshot.zip",
    "backend/nested/snapshot.7z", "backend/nested/private.log", "backend/nested/private.log.1",
    "backend/nested/private.log.gz", "backend/backups/anything.txt",
}
ALLOWED = {
    ".env.example", "backend/.env.example", "backend/app.py", "backend/uv.lock", "frontend/package.json",
    "backend/app/services/backups/storage.py", "backend/example.dbnotes",
}


def run(args, *, env, cwd, timeout=180, check=True):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"docker command failed: {result.returncode}")
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", action="store_true", help="使用旧版 Docker 构建器对照")
    options = parser.parse_args()
    env = {key: os.environ[key] for key in PASSTHROUGH if key in os.environ}
    if options.legacy:
        env["DOCKER_BUILDKIT"] = "0"
    run_id = secrets.token_hex(8)
    tag = f"ainovel-c7-context-{run_id}"
    label = f"ainovel.c7.context={run_id}"
    name = f"c7context{run_id}"
    tag_filter = f"reference={tag}"
    label_filter = f"label={label}"
    for args in (["docker", "image", "ls", "-q", "--filter", tag_filter],
                 ["docker", "ps", "-aq", "--filter", label_filter],
                 ["docker", "ps", "-aq", "--filter", f"name=^/{name}$"]):
        if run(args, env=env, cwd=ROOT):
            raise RuntimeError("test resource name already exists")

    attempted = False
    uncertain = False
    try:
        with tempfile.TemporaryDirectory(prefix="c7-build-context-") as temporary:
            context = Path(temporary)
            (context / ".dockerignore").write_bytes((ROOT / ".dockerignore").read_bytes())
            (context / "Dockerfile").write_text("FROM postgres:16-alpine\nCOPY . /probe/\n")
            for filename in BLOCKED | ALLOWED:
                path = context / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic-test-data\n")
            attempted = True
            try:
                command = ["docker", "build", "--network", "none", "--no-cache", "--rm", "--force-rm",
                           "--label", label, "--tag", tag]
                if not options.legacy:
                    command.extend(["--progress", "plain"])
                build = subprocess.run([*command, "."], env=env, cwd=context,
                                       capture_output=True, text=True, timeout=300)
                if build.returncode:
                    raise RuntimeError(f"docker build failed: {build.returncode}")
                if not options.legacy and " [internal] load build definition" not in build.stdout + build.stderr:
                    raise RuntimeError("default build did not report BuildKit")
                output = run(["docker", "run", "--rm", "--name", name, "--label", label, "--network", "none",
                              "--tmpfs", "/var/lib/postgresql/data", "--entrypoint", "sh", tag,
                              "-c", "find /probe -type f -print"], env=env, cwd=context, timeout=60)
            except BaseException:
                uncertain = True
                raise
            files = {line.removeprefix("/probe/") for line in output.splitlines()}
            missing = ALLOWED - files
            leaked = BLOCKED & files
            if missing or leaked:
                raise AssertionError(f"fake context mismatch: missing={sorted(missing)} included={sorted(leaked)}")
            mode = "旧版构建器" if options.legacy else "默认 BuildKit"
            print(f"{mode}虚构文件验证通过：{len(BLOCKED)} 个敏感文件被排除，{len(ALLOWED)} 个例子和源码被保留")
    finally:
        if attempted:
            try:
                for container in run(["docker", "ps", "-aq", "--filter", label_filter], env=env, cwd=ROOT).splitlines():
                    run(["docker", "rm", "-f", "-v", container], env=env, cwd=ROOT)
                if run(["docker", "image", "ls", "-q", "--filter", tag_filter], env=env, cwd=ROOT):
                    run(["docker", "image", "rm", tag], env=env, cwd=ROOT)
                if run(["docker", "ps", "-aq", "--filter", label_filter], env=env, cwd=ROOT) or run(
                    ["docker", "image", "ls", "-q", "--filter", tag_filter], env=env, cwd=ROOT,
                ):
                    raise RuntimeError("test resources remain")
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                uncertain = True
            if uncertain:
                print(f"测试资源清理未确认；请核对本轮镜像标签 {tag} 和资源标签 {label}", file=sys.stderr)
                raise RuntimeError("cleanup unconfirmed") from None


if __name__ == "__main__":
    main()
