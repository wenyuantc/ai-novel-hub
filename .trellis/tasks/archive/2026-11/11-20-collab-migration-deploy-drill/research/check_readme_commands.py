"""隔离验证 README 的快照示例；不读取配置或连接真实数据库。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def repository() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "README.md").is_file() and (parent / "backend").is_dir():
            return parent
    raise RuntimeError("无法定位项目目录")


def shell_blocks(document: str) -> list[str]:
    result = []
    for match in re.finditer(r"(?ms)^( {0,3})```(?:bash|sh)\s*\n(.*?)^\1```\s*$", document):
        indent = len(match.group(1))
        result.append(
            "\n".join(line[indent:] if line.startswith(" " * indent) else line for line in match.group(2).splitlines())
            + "\n"
        )
    return result


def heredoc(block: str) -> str:
    return block.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


def run_python(script: str, *args: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, *(str(arg) for arg in args)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def sqlite_snapshot_checks(root: Path, block: str, hash_block: str) -> list[str]:
    script = heredoc(block)
    db = root / "source ' and spaces.sqlite"
    target = root / "snapshot ' and spaces.sqlite"
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('0043')")
        connection.execute("CREATE TABLE example(id INTEGER PRIMARY KEY, text_value TEXT)")
        connection.execute("INSERT INTO example VALUES (1, 'committed WAL data')")
        connection.commit()
        assert Path(f"{db}-wal").stat().st_size > 0

        run = run_python(script, db, target, "0043")
        assert run.returncode == 0, run.stderr
        assert not any(Path(f"{target}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))
        with sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True) as restored:
            assert restored.execute("SELECT * FROM example").fetchall() == [(1, "committed WAL data")]
            assert restored.execute("SELECT version_num FROM alembic_version").fetchall() == [("0043",)]
        before = target.read_bytes()
        assert run_python(script, db, target, "0043").returncode != 0
        assert target.read_bytes() == before
        assert run_python(script, db, db, "0043").returncode != 0

        connection.execute("UPDATE alembic_version SET version_num = '0070'")
        connection.commit()
        wrong_version = root / "wrong-version.sqlite"
        assert run_python(script, db, wrong_version, "0043").returncode != 0
        assert not wrong_version.exists()
        new_snapshot = root / "new-version.sqlite"
        assert run_python(script, db, new_snapshot, "0070").returncode == 0
        with sqlite3.connect(f"{new_snapshot.as_uri()}?mode=ro", uri=True) as restored:
            assert restored.execute("SELECT version_num FROM alembic_version").fetchall() == [("0070",)]
            assert restored.execute("SELECT * FROM example").fetchall() == [(1, "committed WAL data")]

        output = run_python(heredoc(hash_block), target)
        assert output.returncode == 0
        assert output.stdout.strip() == hashlib.sha256(target.read_bytes()).hexdigest()
    finally:
        connection.close()
    return [
        "WAL",
        "单文件",
        "带空格及引号路径",
        "拒绝覆盖",
        "拒绝同路径",
        "拒绝不匹配版本",
        "备份0070",
        "SHA-256",
    ]


def postgres_command_checks(root: Path, block: str) -> list[str]:
    fakebin = root / "bin"
    fakebin.mkdir()
    docker = fakebin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$C8_FAKE_LOG"\n'
        'case "$*" in\n'
        "  'compose stop app') [ \"$C8_FAKE_FAILURE\" != stop ] ;;\n"
        "  'compose exec -T db '*) [ \"$C8_FAKE_FAILURE\" != dump ] || exit 17; "
        "[ \"$C8_FAKE_FAILURE\" = empty ] || printf 'fake dump';;\n"
        "  *) exit 18;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    for failure in ("none", "stop", "dump", "empty"):
        case = root / failure
        case.mkdir()
        env = {
            "PATH": f"{fakebin}:/usr/bin:/bin",
            "C8_FAKE_LOG": str(case / "calls.log"),
            "C8_FAKE_FAILURE": failure,
        }
        result = subprocess.run(
            ["bash", "-eu"],
            cwd=case,
            env=env,
            input=block,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        target = case / "backups/ainovel-before-collaboration.dump"
        assert (result.returncode == 0) == (failure == "none"), (failure, result.stderr)
        assert ("最终快照已生成" in result.stdout) == (failure == "none")
        if failure == "none":
            assert target.read_text() == "fake dump"
            retry = subprocess.run(
                ["bash", "-eu"],
                cwd=case,
                env=env,
                input=block,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            assert retry.returncode != 0 and target.read_text() == "fake dump"
        if failure == "stop":
            assert not target.exists()
    return ["正常", "拒绝覆盖", "停服失败", "备份失败", "空输出"]


def main() -> None:
    readme = (repository() / "README.md").read_bytes()
    blocks = shell_blocks(readme.decode())
    assert blocks
    for number, block in enumerate(blocks, 1):
        result = subprocess.run(
            ["bash", "-n"],
            input=block,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, f"README shell block {number}: {result.stderr}"
    sqlite = next(block for block in blocks if "before-collaboration.sqlite3" in block)
    postgres = next(block for block in blocks if "ainovel-before-collaboration.dump" in block)
    hash_block = next(block for block in blocks if "snapshot=/absolute/path/to/final-snapshot" in block)
    with TemporaryDirectory(prefix="ainovel-c8-readme-check-") as temporary:
        root = Path(temporary)
        outcomes = {
            "readme_sha256": hashlib.sha256(readme).hexdigest(),
            "shell_blocks_checked": len(blocks),
            "sqlite": sqlite_snapshot_checks(root, sqlite, hash_block),
            "postgres_fake_docker": postgres_command_checks(root, postgres),
        }
    print(json.dumps(outcomes, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
