"""校验成功证据只保留许可字段，且不覆盖既有证据。"""

from __future__ import annotations

import json
import runpy
import secrets
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
recorder = runpy.run_path(str(HERE / "record_success.py"))
safe_case = recorder["safe_case"]
write_result = recorder["write_result"]


def verify():
    secret = secrets.token_urlsafe(32)
    world = SimpleNamespace(
        topology="sqlite",
        processes=[
            SimpleNamespace(url=f"http://user:{secret}@127.0.0.1:12341/?body={secret}",
                            process=SimpleNamespace(pid=101, returncode=None)),
            SimpleNamespace(url="http://127.0.0.1:12342", process=SimpleNamespace(pid=102, returncode=None)),
        ],
    )
    original = {
        "case": "forward",
        "executor_pid": 101,
        "other_pid": 102,
        "events": ["snapshot", "done"],
        "request_body": secret,
        "model_content": secret,
        "token": secret,
    }
    report = safe_case(world, original)
    encoded = json.dumps(report, ensure_ascii=False)
    assert secret not in encoded
    assert report["endpoints"]["executor_pid"] == {"pid": 101, "public_port": 12341}
    assert report["endpoints"]["other_pid"] == {"pid": 102, "public_port": 12342}
    try:
        safe_case(world, {**original, "events": [secret]})
    except AssertionError:
        pass
    else:
        raise AssertionError("非白名单事件未被拒绝")
    with TemporaryDirectory(prefix="c7-success-redaction-") as directory:
        path = Path(directory) / "result.json"
        write_result(path, report)
        assert secret not in path.read_text(encoding="utf-8")
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
        try:
            write_result(path, {"must_not_replace": True})
        except FileExistsError:
            pass
        else:
            raise AssertionError("已有证据被覆盖")
        assert json.loads(path.read_text(encoding="utf-8")) == report
    print("success_evidence_redacted=true; existing_evidence_preserved=true")


if __name__ == "__main__":
    verify()
