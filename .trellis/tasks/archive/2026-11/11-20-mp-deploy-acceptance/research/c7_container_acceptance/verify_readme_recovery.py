"""用假 Docker 验证 README 恢复命令在每一步失败时不会继续或启动应用。"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FAKE_DOCKER = r'''#!/bin/sh
case " $* " in
  *" stop "*) stage=stop ;;
  *" up -d --wait db "*) stage=wait_db ;;
  *" createdb "*) stage=createdb ;;
  *" pg_restore "*) stage=restore ;;
  *" psql "*) stage=check ;;
  *" config --format json "*) stage=config ;;
  *" up -d --no-build "*) stage=start_app ;;
  *) exit 98 ;;
esac
printf '%s\n' "$stage" >> "$C7_FAKE_LOG"
failure="$stage"
if [ "$stage" = wait_db ] && [ "$(grep -c '^wait_db$' "$C7_FAKE_LOG")" = 2 ]; then
  failure=wait_db_after_restore
fi
if [ "$failure" = "$C7_FAIL_STAGE" ]; then exit 17; fi
if [ "$stage" = config ]; then
  target=ainovel_restore_20261125
  app_target="$target"
  app_host=db
  app_password=fake
  case "$C7_BAD_TARGET" in
    database) target=wrong_db ;;
    app_db) app_target=wrong_db ;;
    app_host) app_host=wrong_host ;;
    app_password) app_password=wrong_password ;;
  esac
  printf '{"services":{"db":{"environment":{"POSTGRES_DB":"%s","POSTGRES_USER":"fake",' "$target"
  printf '"POSTGRES_PASSWORD":"fake"}},"app":{"environment":{'
  printf '"DATABASE_URL":"postgresql+asyncpg://fake:%s@%s:5432/%s"}}}}\n' \
    "$app_password" "$app_host" "$app_target"
fi
exit 0
'''


def recovery_blocks(readme):
    text = readme.read_text(encoding="utf-8")
    start = text.index("在同一个 Bash 会话中")
    end = text.index("后台下载的", start)
    blocks = re.findall(r"```bash\n(.*?)\n```", text[start:end], re.DOTALL)
    if len(blocks) != 2 or "restore_ok=0" not in blocks[0] or "restore_ok:-0" not in blocks[1]:
        raise AssertionError("README 恢复代码块结构不符合预期")
    return blocks


def verify_compose_targets(start, directory):
    """用虚构配置核对真实 Compose 解析和 README 的目标校验代码。"""
    root = next(path for path in Path(__file__).resolve().parents if (path / "backend/alembic.ini").is_file())
    match = re.search(r"python3 -c '\n(.*?)\n' \"\$restore_db\"", start, re.DOTALL)
    if match is None:
        raise AssertionError("README 中缺少 Compose 目标校验")
    checker = match.group(1)
    env_file = directory / "isolated.env"
    env_file.write_text(
        "POSTGRES_USER=fake\nPOSTGRES_PASSWORD=fake\nPOSTGRES_DB=ainovel_restore_20261125\n"
        "JWT_SECRET=fake-test-key\nENCRYPTION_KEY=fake-test-key\n", encoding="utf-8",
    )
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}
    for compose_file, apps in (("docker-compose.yml", ["app"]), ("docker-compose.multi.yml", ["app1", "app2"])):
        compose = [
            "docker", "compose", "--env-file", str(env_file), "-p", "c7docconfig", "-f", str(root / compose_file),
        ]
        override = directory / "override.yml"
        override.write_text(
            f'services:\n  {apps[-1]}:\n    environment:\n      DATABASE_URL: '
            '"postgresql+asyncpg://fake:fake@db:5432/wrong_db"\n', encoding="utf-8",
        )
        cases = (
            (compose, environment, True),
            (compose, {**environment, "POSTGRES_DB": "wrong_db"}, False),
            ([*compose, "-f", str(override)], environment, False),
        )
        for command, env, expected in cases:
            config = subprocess.run([*command, "config", "--format", "json"], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=30, check=True)
            result = subprocess.run([sys.executable, "-c", checker, "ainovel_restore_20261125", *apps],
                                    input=config.stdout, capture_output=True, text=True, timeout=30, check=False)
            assert (result.returncode == 0) is expected
            assert "fake-test-key" not in result.stdout + result.stderr
    print("真实 Compose 解析：单实例与双实例的正确目标通过，环境覆盖或应用目标不一致均被拒绝")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readme", type=Path, required=True)
    parser.add_argument("--verify-compose", action="store_true")
    args = parser.parse_args()
    restore, start = recovery_blocks(args.readme)
    expected = ["stop", "wait_db", "createdb", "restore", "check", "config", "wait_db", "start_app"]
    with tempfile.TemporaryDirectory(prefix="c7-readme-recovery-") as temporary:
        root = Path(temporary)
        fakebin = root / "bin"
        fakebin.mkdir()
        docker = fakebin / "docker"
        docker.write_text(FAKE_DOCKER, encoding="utf-8")
        docker.chmod(0o700)
        backup = root / "backups/ainovel-YYYYMMDD-HHMMSS.dump"
        backup.parent.mkdir()
        backup.write_bytes(b"synthetic")
        failures = ("stop", "wait_db", "createdb", "restore", "check", "config", "database", "app_db",
                    "app_host", "app_password", "wait_db_after_restore", "none")
        for failure in failures:
            log = root / f"{failure}.log"
            environment = {
                "PATH": f"{fakebin}:/usr/bin:/bin",
                "C7_FAKE_LOG": str(log),
                "C7_FAIL_STAGE": failure,
                "C7_BAD_TARGET": failure,
                "LC_ALL": "C",
            }
            result = subprocess.run(["bash", "-c", restore + "\n" + start], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=30, check=False)
            observed = log.read_text(encoding="utf-8").splitlines()
            if failure == "none":
                assert result.returncode == 0 and observed == expected, (failure, result.returncode, observed)
            else:
                if failure == "wait_db_after_restore":
                    limit = 6
                elif failure in {"database", "app_db", "app_host", "app_password"}:
                    limit = expected.index("config")
                else:
                    limit = expected.index(failure)
                assert result.returncode != 0 and observed == expected[:limit + 1], (
                    failure, result.returncode, observed,
                )
        if args.verify_compose:
            verify_compose_targets(start, root)
    print("README 恢复命令：七个失败阶段及四种错误目标均阻断后续操作与启动；成功路径顺序正确")


if __name__ == "__main__":
    main()
