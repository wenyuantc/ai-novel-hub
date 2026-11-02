"""真实隔离验收入口：uv run python tests/backup_acceptance.py --scenario all --output DIR。

先在仓库根构建 ainovel-backup-acceptance:local。所有数据库/密钥/产物在随机
0700 临时目录和专有 Docker 网络/卷内，退出回收；输出目录只接收脱敏检查事实。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

# 直接执行 tests/ 下脚本时，保证导入本仓库 tests 而不是第三方同名包。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.backup_acceptance.infra import (  # noqa: E402
    AcceptanceFailure,
    DockerLab,
    command,
    require,
)

BACKUPS = "/api/v1/admin/backups"
TERMINAL = {"succeeded", "partial", "failed", "interrupted"}


class Http:
    def __init__(self, url: str, lab: DockerLab):
        self.client = httpx.Client(base_url=url, timeout=30, trust_env=False)
        self.lab = lab

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, expected: int = 200, **kwargs: Any) -> Any:
        response = self.client.request(method, path, **kwargs)
        require(response.status_code == expected, f"http_{method}_{path.split('?')[0]}_{expected}")
        require(not any(
            value in response.text or json.dumps(value, ensure_ascii=False)[1:-1] in response.text
            for value in self.lab.secrets_to_check
        ), "api_secret_not_returned")
        if path.startswith(BACKUPS):
            require(response.headers.get("cache-control") == "no-store", "backup_api_no_store")
        return response.json() if response.content else None

    def ready(self, url: str | None = None) -> None:
        if url is not None:
            # Docker Desktop的随机publish在restart后可能改变；Cookie仍按同一个127.0.0.1域保留。
            self.client.base_url = httpx.URL(url)
        until = time.monotonic() + 120
        while time.monotonic() < until:
            try:
                if self.client.get("/healthz").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise AcceptanceFailure("application_ready")

    def login(self, username: str, password: str) -> dict[str, Any]:
        return self.request("POST", "/api/v1/auth/login", json={"username": username, "password": password})

    def configure(self, **changes: Any) -> dict[str, Any]:
        current = self.request("GET", f"{BACKUPS}/settings")
        return self.request("PATCH", f"{BACKUPS}/settings", json={"revision": current["revision"], "changes": changes})

    def run(self, request_id: str | None = None) -> dict[str, Any]:
        overview = self.request("GET", BACKUPS)
        accepted = self.request("POST", f"{BACKUPS}/runs", expected=202, json={
            "request_id": request_id or str(uuid4()), "settings_revision": overview["revision"],
        })
        return self.wait(accepted["id"])

    def wait(self, run_id: int) -> dict[str, Any]:
        until = time.monotonic() + 120
        while time.monotonic() < until:
            result = self.request("GET", f"{BACKUPS}/runs/{run_id}")
            if result["status"] in TERMINAL:
                return result
            time.sleep(0.2)
        raise AcceptanceFailure("backup_reaches_terminal")

    def download(self, run: dict[str, Any], destination: str, path: Path) -> None:
        url = f"{BACKUPS}/runs/{run['id']}/download?destination={destination}"
        artifact = next(item for item in run["artifacts"] if item["destination"] == destination)
        head = self.client.head(url)
        require(head.status_code == 200, "download_head_authorized")
        require(head.headers.get("cache-control") == "no-store", "download_head_no_store")
        digest = hashlib.sha256()
        size = 0
        with self.client.stream("GET", url) as response:
            require(response.status_code == 200, "download_get_authorized")
            require(response.headers.get("cache-control") == "no-store", "download_get_no_store")
            require(response.headers.get("x-content-type-options") == "nosniff", "download_nosniff")
            require("attachment" in response.headers.get("content-disposition", ""), "download_attachment")
            with path.open("xb") as target:
                path.chmod(0o600)
                for chunk in response.iter_bytes(64 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    target.write(chunk)
            require(int(response.headers["content-length"]) == size, "download_content_length")
        require(size == artifact["size_bytes"], "download_artifact_length")
        require(digest.hexdigest() == artifact["checksum_sha256"], "download_artifact_checksum")


class Acceptance:
    def __init__(self, lab: DockerLab):
        self.lab = lab
        self.checks: list[dict[str, Any]] = []
        self.clients: list[Http] = []
        self.delegates: dict[str, dict[str, tuple[Http, int]]] = {}

    def check(self, name: str, **facts: Any) -> None:
        self.checks.append({"check": name, "status": "passed", **facts})
        print(f"PASS {name}", flush=True)

    def expected_secrets(self, backup_password: str | None = None) -> dict[str, str]:
        return {
            "backup_password": backup_password or self.lab.backup_password,
            "s3_access_key": self.lab.s3_access, "s3_secret_key": self.lab.s3_secret,
            "smtp_username": self.lab.smtp_username, "smtp_password": self.lab.smtp_password,
        }

    def client(self, url: str) -> Http:
        result = Http(url, self.lab)
        self.clients.append(result)
        return result

    def close(self) -> None:
        for client in self.clients:
            client.close()

    def seed(self, api: Http, url: str, engine: str) -> dict[str, Any]:
        owner = self.client(url)
        user = owner.request("POST", "/api/v1/auth/register", expected=201, json={
            "username": f"owner_{engine}", "password": self.lab.admin_password,
        })
        project = owner.request("POST", "/api/v1/projects", expected=201, json={
            "title": f"整库恢复验收-{engine}", "premise": "仅隔离合成数据", "tags": ["验收"],
        })
        chapter = owner.request("POST", f"/api/v1/projects/{project['id']}/chapters", expected=201, json={
            # 不可压缩的合成内容让真实产物跨过8MiB multipart阈值，验证大文件路径。
            "chapter_no": 1, "title": "合成章节",
            "content": "完整快照应保存作品内容与引用。\n" + secrets.token_urlsafe(9 * 1024 * 1024),
        })
        owner.request("POST", "/api/v1/ai-configs", expected=201, json={
            "name": "隔离凭据", "provider": "openai_compat", "model": "synthetic-model",
            "api_key": self.lab.provider_secret, "is_default": True,
        })
        # 上传真实小图片，让数据库大二进制与项目关联也参与恢复比较。
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        owner.request("PUT", f"/api/v1/projects/{project['id']}/cover", files={"file": ("cover.png", png, "image/png")})
        settings = api.request("GET", "/api/v1/admin/system-settings")
        api.request("PATCH", "/api/v1/admin/system-settings", json={
            "revision": settings["revision"], "changes": {"auto_analyze_enabled": False, "task_max_per_user": 3},
        })
        return {"owner": user["id"], "project": project["id"], "chapter": chapter["id"]}

    def permission_matrix(self, api: Http, url: str, engine: str) -> None:
        self.delegates[engine] = {}
        guest = self.client(url)
        guest.request("GET", BACKUPS, expected=401)
        for label, permissions in (
            ("none", []), ("read", ["backups.read"]),
            ("configure", ["backups.read", "backups.configure"]),
            ("run", ["backups.read", "backups.run"]),
            ("download", ["backups.read", "backups.download"]),
        ):
            person = self.client(url)
            user = person.request("POST", "/api/v1/auth/register", expected=201, json={
                "username": f"{engine}_{label}", "password": self.lab.admin_password,
            })
            self.delegates[engine][label] = (person, user["id"])
            if permissions:
                role = api.request("POST", "/api/v1/admin/roles", expected=201, json={
                    "name": f"{engine}_{label}", "permissions": permissions,
                })
                catalog = api.request("GET", "/api/v1/admin/roles")
                api.request("PUT", f"/api/v1/admin/users/{user['id']}/roles", json={
                    "authorization_revision": catalog["authorization_revision"], "role_ids": [role["id"]],
                })
            person.request("GET", BACKUPS, expected=200 if permissions else 403)
            person.request("GET", f"{BACKUPS}/settings", expected=200 if label == "configure" else 403)
            if label == "configure":
                current = person.request("GET", f"{BACKUPS}/settings")
                person.request("PATCH", f"{BACKUPS}/settings", json={
                    "revision": current["revision"], "changes": {"local_keep": 7},
                })
            if label == "run":
                overview = person.request("GET", BACKUPS)
                # 尚无加密密码时返回就绪错误400，证明委派执行动作通过授权且没有降为明文。
                denied = person.request("POST", f"{BACKUPS}/runs", expected=400, json={
                    "request_id": str(uuid4()), "settings_revision": overview["revision"],
                })
                require(denied.get("code") == "backup_not_ready", "delegated_run_checked_readiness")
            else:
                person.request("POST", f"{BACKUPS}/runs", expected=403, json={
                    "request_id": str(uuid4()), "settings_revision": 0,
                })
            person.request(
                "HEAD", f"{BACKUPS}/runs/2147483647/download?destination=local",
                expected=404 if label == "download" else 403,
            )
            if label == "read":
                roles = api.request("GET", "/api/v1/admin/roles")
                user_role = next(item for item in roles["items"] if item["system_key"] == "user")
                api.request("PUT", f"/api/v1/admin/users/{user['id']}/roles", json={
                    "authorization_revision": roles["authorization_revision"], "role_ids": [user_role["id"]],
                })
                person.request("GET", BACKUPS, expected=403)
        self.check(f"{engine}.http_permissions_and_revocation")

    def configure_initial(self, api: Http, engine: str) -> None:
        initial = api.request("GET", f"{BACKUPS}/settings")
        require(initial["schedule_time"] == "03:00", "default_beijing_time")
        require(initial["timezone"] == "Asia/Shanghai", "default_timezone")
        require(not initial["schedule_enabled"] and initial["encryption_enabled"], "safe_initial_switches")
        require(initial["local_keep"] == initial["s3_keep"] == 7, "default_independent_retention")
        api.configure(
            schedule_enabled=False, local_enabled=True, s3_enabled=False, encryption_enabled=False,
            backup_password=self.lab.backup_password, s3_endpoint="https://s3.acceptance.test:9000",
            s3_region="us-east-1", s3_bucket="acceptance-backups", s3_prefix=f"{engine}",
            s3_access_key=self.lab.s3_access, s3_secret_key=self.lab.s3_secret,
            smtp_host="smtp.acceptance.test", smtp_port=465, smtp_security="ssl",
            smtp_from_address="backup@example.com", smtp_recipients=["admin@example.com"],
            smtp_username=self.lab.smtp_username, smtp_password=self.lab.smtp_password,
        )
        api.request("PATCH", f"{BACKUPS}/settings", expected=409, json={
            "revision": initial["revision"], "changes": {"local_keep": 3},
        })
        current = api.request("GET", f"{BACKUPS}/settings")
        require(current["backup_password_configured"], "backup_password_configured_flag")
        api.configure(local_keep=7)
        require(api.request("GET", f"{BACKUPS}/settings")["backup_password_configured"], "omitted_secret_preserved")
        invalid_secret = "invalid-" + secrets.token_hex(12) + "\n"
        self.lab.secrets_to_check.append(invalid_secret)
        api.request("PATCH", f"{BACKUPS}/settings", expected=422, json={
            "revision": api.request("GET", BACKUPS)["revision"], "changes": {"backup_password": invalid_secret},
        })
        self.check(f"{engine}.configuration_defaults_cas_and_secret_omission")
        runner, runner_id = self.delegates[engine]["run"]
        revision = api.request("GET", BACKUPS)["revision"]
        request = {"request_id": str(uuid4()), "settings_revision": revision}
        accepted = runner.request("POST", f"{BACKUPS}/runs", expected=202, json=request)
        delegated = runner.wait(accepted["id"])
        require(delegated["status"] == "succeeded", "delegated_run_real_success")
        require(delegated["actor_user_id"] == runner_id, "delegated_run_keeps_real_actor")
        api.request("POST", f"{BACKUPS}/runs", expected=409, json=request)
        downloader, _ = self.delegates[engine]["download"]
        downloader.download(delegated, "local", self.lab.root / f"{engine}/delegated-download.bin")
        self.check(f"{engine}.delegated_execute_download_and_cross_actor_request_conflict")

    def restore(
        self, api: Http, container: str, engine: str, run: dict[str, Any], baseline: dict[str, Any],
        *, password: str | None = None,
    ) -> None:
        password = password or self.lab.backup_password
        extension = "dump" if engine == "postgresql" else "sqlite3"
        name = f"{engine}/download-{run['id']}.{extension}" + (".gpg" if run["encrypted"] else "")
        api.download(run, "local", self.lab.root / name)
        artifact = next(item for item in run["artifacts"] if item["destination"] == "local")
        if run["encrypted"]:
            require(artifact["filename"].endswith(f".{extension}.gpg"), "encrypted_filename")
            wrong = self.lab.probe(container, "decrypt", {
                "source": name, "destination": f"{engine}/wrong-{run['id']}.{extension}",
                "password": "incorrect-acceptance-password",
            })
            require(not wrong["accepted"], "gpg_wrong_password_rejected")
            corrupt = self.lab.root / f"{engine}/corrupt-{run['id']}.gpg"
            value = (self.lab.root / name).read_bytes()
            corrupt.write_bytes(value[: max(1, len(value) // 2)])
            corrupt.chmod(0o600)
            broken = self.lab.probe(container, "decrypt", {
                "source": str(corrupt.relative_to(self.lab.root)),
                "destination": f"{engine}/broken-{run['id']}.{extension}", "password": password,
            })
            require(not broken["accepted"], "gpg_corrupt_file_rejected")
            output = f"{engine}/decrypted-{run['id']}.{extension}"
            require(self.lab.probe(container, "decrypt", {
                "source": name, "destination": output, "password": password,
            })["accepted"], "gpg_correct_password_accepted")
            name = output
        else:
            require(artifact["filename"].endswith(f".{extension}"), "plain_filename")
        if engine == "postgresql":
            restore_db = f"restore_{run['id']}"
            self.lab.exec(self.lab.pg, "createdb", "--username", self.lab.pg_user, restore_db)
            command("docker", "cp", str(self.lab.root / name), f"{self.lab.pg}:/tmp/restore.dump")
            self.lab.exec(
                self.lab.pg, "pg_restore", "--username", self.lab.pg_user, "--dbname", restore_db,
                "--no-owner", "--exit-on-error", "/tmp/restore.dump", check="postgres_restore_real",
            )
            restored_url = (
                f"postgresql+asyncpg://{self.lab.pg_user}:{self.lab.pg_password}"
                f"@pg.acceptance.test:5432/{restore_db}"
            )
        else:
            restored_url = f"sqlite+aiosqlite:////acceptance-data/{name}"
        restored = self.lab.probe(container, "fingerprint", {
            "database_url": restored_url, "provider_secret": self.lab.provider_secret,
        })
        require(restored == baseline, "restored_business_data_references_and_credentials_equal")
        require(all(self.lab.probe(container, "secret_storage", {
            "database_url": restored_url, "secrets": self.expected_secrets(password),
        }).values()), "restored_backup_credentials_ciphertext_and_original_key_decryption")
        target, target_url = self.lab.start_restored(engine, run["id"], restored_url)
        target_api = self.client(target_url)
        try:
            target_api.ready()
            target_api.login(f"owner_{engine}", self.lab.admin_password)
            projects = target_api.request("GET", "/api/v1/projects")
            require(len(projects) == 1 and projects[0]["title"] == f"整库恢复验收-{engine}", "restored_app_projects")
            chapters = target_api.request("GET", f"/api/v1/projects/{projects[0]['id']}/chapters")
            require(len(chapters) == 1 and chapters[0]["word_count"] > 0, "restored_app_chapter_references")
            configs = target_api.request("GET", "/api/v1/ai-configs")
            require(len(configs) == 1 and configs[0]["name"] == "隔离凭据", "restored_app_private_configuration")
        finally:
            command("docker", "stop", "--time", "15", target, check="stop_owned_restored_app")
        mode = "plain" if not run["encrypted"] else "encrypted"
        if password != self.lab.backup_password:
            mode = "encrypted_rotated"
        self.check(
            f"{engine}.{mode}_real_download_restore",
            tables=len(restored["tables"]), credential_decrypts=restored["credential_decrypts"],
            integrity=restored["integrity"], schema=restored["schema"],
            artifact_sha256=artifact["checksum_sha256"], restored_app_login_and_read=True,
        )

    def roundtrip(self, api: Http, container: str, engine: str) -> None:
        if engine == "sqlite":
            require(
                self.lab.probe(container, "wal_marker", {"marker": secrets.token_urlsafe(32)})["wal_mode"], "wal_real",
            )
        baseline = self.lab.probe(container, "fingerprint", {"provider_secret": self.lab.provider_secret})
        require(baseline["credential_decrypts"] and baseline["integrity"], "source_valid")
        require(all(baseline["tables"][name]["rows"] > 0 for name in (
            "users", "roles", "user_roles", "system_settings", "ai_configs", "projects", "project_covers", "chapters",
        )), "synthetic_business_coverage")
        for encrypted in (False, True):
            api.configure(encryption_enabled=encrypted, local_enabled=True, s3_enabled=False)
            result = api.run()
            require(
                result["status"] == "succeeded" and result["encrypted"] is encrypted,
                f"roundtrip_{engine}_{encrypted}_{result['status']}_{result['error_code']}",
            )
            require(len(result["artifacts"]) == 1, "disabled_s3_no_artifact")
            self.restore(api, container, engine, result, baseline)
        old_encrypted = result
        new_password = secrets.token_urlsafe(36)
        self.lab.secrets_to_check.append(new_password)
        api.configure(backup_password=new_password, encryption_enabled=False)
        old_record = api.request("GET", f"{BACKUPS}/runs/{old_encrypted['id']}")
        require(old_record["encrypted"] is True, "old_actual_format_unchanged")
        old_download = f"{engine}/old-after-password-rotation.gpg"
        api.download(old_record, "local", self.lab.root / old_download)
        require(not self.lab.probe(container, "decrypt", {
            "source": old_download, "destination": f"{engine}/old-with-new-password.bin", "password": new_password,
        })["accepted"], "old_file_does_not_use_new_password")
        require(self.lab.probe(container, "decrypt", {
            "source": old_download, "destination": f"{engine}/old-with-original-password.bin",
            "password": self.lab.backup_password,
        })["accepted"], "old_file_original_password_still_works")
        api.configure(encryption_enabled=True)
        new_encrypted = api.run()
        require(new_encrypted["status"] == "succeeded", "rotated_password_backup_succeeded")
        self.restore(api, container, engine, new_encrypted, baseline, password=new_password)
        api.configure(backup_password=self.lab.backup_password)
        self.check(f"{engine}.password_and_format_rotation_only_affects_new_artifacts")

    def storage(self, api: Http, container: str, engine: str) -> None:
        api.configure(encryption_enabled=False, local_enabled=False, s3_enabled=True, local_keep=1, s3_keep=2)
        only_s3 = api.run()
        require(only_s3["status"] == "succeeded", "s3_only_succeeded")
        require({item["destination"] for item in only_s3["artifacts"]} == {"s3"}, "local_disabled_no_artifact")
        path = self.lab.root / f"{engine}/s3-only.bin"
        api.download(only_s3, "s3", path)
        api.configure(local_enabled=True, s3_enabled=True)
        runs = [api.run(), api.run(), api.run()]
        require(all(run["status"] == "succeeded" for run in runs), "dual_storage_succeeded")
        records = api.request("GET", f"{BACKUPS}/runs?page_size=100")["items"]
        ready_local = [
            a for r in records for a in r["artifacts"] if a["destination"] == "local" and a["status"] == "ready"
        ]
        ready_s3 = [a for r in records for a in r["artifacts"] if a["destination"] == "s3" and a["status"] == "ready"]
        require(len(ready_local) == 1 and len(ready_s3) == 2, "independent_retention_counts")
        require(all(item["size_bytes"] > 8 * 1024 * 1024 for item in ready_s3), "real_large_artifacts")
        versions = self.lab.probe(container, "s3", {
            "operation": "versions", "bucket": "acceptance-backups", "access_key": self.lab.s3_access,
            "secret_key": self.lab.s3_secret, "prefix": f"{engine}/",
        })
        require(
            versions["versions"] == versions["multipart_versions"] == 2
            and versions["version_ids_present"] and versions["delete_markers"] == 0,
            "exact_version_deletion",
        )
        old = runs[-1]
        api.configure(s3_bucket="acceptance-secondary")
        old_path = self.lab.root / f"{engine}/old-location.bin"
        api.download(old, "s3", old_path)
        api.configure(s3_access_key=None, s3_secret_key=None)
        # 旧产物保存加密访问快照；当前位置不完整不影响已生成产物的下载。
        api.download(old, "s3", self.lab.root / f"{engine}/old-credentials.bin")
        api.configure(s3_access_key=self.lab.s3_access, s3_secret_key=self.lab.s3_secret)
        self.check(f"{engine}.s3_switches_retention_versioned_cleanup_old_location", **versions)
        api.configure(local_enabled=True, s3_enabled=False, local_keep=1)
        artifact = next(item for item in old["artifacts"] if item["destination"] == "local")
        matches = list((self.lab.root / engine / "backups").rglob(artifact["filename"]))
        require(len(matches) == 1, "own_local_artifact_identified")
        original = matches[0]
        held = original.with_suffix(original.suffix + ".acceptance-held")
        original.rename(held)
        unrelated = b"isolated unrelated file must never be removed by retention"
        original.write_bytes(unrelated)
        original.chmod(0o600)
        try:
            cleanup = api.run()
            require(cleanup["status"] == "succeeded", "cleanup_failure_keeps_new_backup_success")
            require(cleanup["cleanup_status"] == "failed", "cleanup_identity_failure_visible")
            require(cleanup["notification_status"] == "sent", "cleanup_failure_real_smtp")
            require(original.read_bytes() == unrelated, "retention_does_not_delete_replaced_file")
            stale = api.request("GET", f"{BACKUPS}/runs/{old['id']}")
            old_local = next(item for item in stale["artifacts"] if item["destination"] == "local")
            require(old_local["status"] != "deleted", "cleanup_failure_not_false_deleted")
        finally:
            original.unlink(missing_ok=True)
            held.rename(original)
        require(api.run()["cleanup_status"] == "succeeded", "cleanup_recovers_precise_original_identity")
        api.request("HEAD", f"{BACKUPS}/runs/{old['id']}/download?destination=local", expected=410)
        self.check(f"{engine}.real_retention_identity_failure_safe_cleanup_recovery_and_expired_download")

    def smtp_failure(self, api: Http, engine: str) -> None:
        api.configure(local_enabled=True, s3_enabled=True, s3_bucket="acceptance-nonexistent")
        partial = api.run()
        require(partial["status"] == "partial", "one_destination_partial")
        require(partial["notification_status"] == "sent", "failure_smtp_accepted")
        api.configure(local_enabled=False)
        failed = api.run()
        require(failed["status"] == "failed", "all_destinations_failed")
        require(failed["notification_status"] == "sent", "all_failed_smtp_accepted")
        captured = self.lab.root / "smtp_messages.jsonl"
        require(captured.is_file(), "real_tls_smtp_captured")
        lines = captured.read_text().splitlines()
        require(len(lines) >= 2, "partial_and_failed_notifications_captured")
        for line in lines:
            raw = json.loads(line)["message"].encode()
            message = BytesParser(policy=policy.default).parsebytes(raw)
            require(message.get("Message-ID"), "smtp_message_id_present")
            body = message.get_body(preferencelist=("plain",))
            text = body.get_content() if body else ""
            require(not any(value in text for value in self.lab.secrets_to_check), "smtp_no_secrets")
            require(not list(message.iter_attachments()), "smtp_no_backup_attachment")
        api.configure(local_enabled=True, s3_enabled=False, smtp_port=466)
        # 本地成功不需要失败邮件；用一处失败目标迫使连接拒绝。
        api.configure(s3_enabled=True)
        rejected = api.run()
        require(rejected["status"] == "partial", "mail_failure_preserves_backup_state")
        require(rejected["notification_status"] in ("failed", "unknown"), "mail_failure_separate_state")
        api.configure(smtp_port=465, s3_enabled=False)
        self.check(f"{engine}.real_tls_smtp_partial_failed_and_mail_failure", captured_messages=len(lines))
        if engine == "sqlite":
            mode = self.lab.root / "smtp_mode.txt"
            mode.write_text("accept_without_ack\n")
            mode.chmod(0o600)
            count = len(captured.read_text().splitlines())
            api.configure(s3_enabled=True)
            unknown = api.run()
            mode.unlink()
            require(unknown["status"] == "partial", "smtp_unknown_keeps_backup_result")
            require(unknown["notification_status"] == "unknown", "accepted_without_ack_is_unknown")
            time.sleep(1.2)
            require(len(captured.read_text().splitlines()) == count + 1, "unknown_mail_not_resent")
            api.configure(s3_enabled=False)
            self.check(f"{engine}.smtp_confirmation_lost_unknown_and_no_blind_retry")

    def idempotency_restart(self, api: Http, container: str, engine: str) -> None:
        api.configure(local_enabled=True, s3_enabled=False, encryption_enabled=False, schedule_enabled=False)
        revision = api.request("GET", BACKUPS)["revision"]
        request = {"request_id": str(uuid4()), "settings_revision": revision}

        def submit() -> dict[str, Any]:
            return api.request("POST", f"{BACKUPS}/runs", expected=202, json=request)

        with ThreadPoolExecutor(max_workers=2) as pool:
            left, right = list(pool.map(lambda _: submit(), range(2)))
        require(left["id"] == right["id"], "same_intent_one_run")
        result = api.wait(left["id"])
        require(result["status"] == "succeeded", "idempotent_run_succeeded")
        settings = api.configure(schedule_time="04:17", local_keep=3, s3_keep=5)
        command("docker", "restart", "--time", "15", container, check="restart_owned_app")
        api.ready(self.lab.app_url(container))
        after = api.request("GET", f"{BACKUPS}/settings")
        require(after == settings, "configuration_persists_restart")
        same = submit()
        require(same["id"] == left["id"], "request_identity_persists_restart")
        self.check(f"{engine}.concurrent_idempotency_and_restart_persistence")

        api.configure(encryption_enabled=True)
        revision = api.request("GET", BACKUPS)["revision"]
        interrupted = api.request("POST", f"{BACKUPS}/runs", expected=202, json={
            "request_id": str(uuid4()), "settings_revision": revision,
        })
        busy = api.request("POST", f"{BACKUPS}/runs", expected=409, json={
            "request_id": str(uuid4()), "settings_revision": revision,
        })
        require(busy.get("code") == "backup_busy", "different_intent_busy_while_real_work_runs")
        command("docker", "kill", "--signal", "KILL", container, check="kill_owned_app_after_acceptance")
        command("docker", "start", container, check="start_owned_app_after_kill")
        api.ready(self.lab.app_url(container))
        recovered = api.wait(interrupted["id"])
        require(recovered["status"] == "interrupted", "accepted_run_interrupted_on_crash")
        require(api.run()["status"] == "succeeded", "lease_released_only_after_restart_recovery")
        require(
            not list((self.lab.root / engine / "backups").rglob(".work-*")),
            "interrupted_private_plaintext_work_removed",
        )
        require(self.lab.probe(container, "private_homes")["remaining"] == 0, "crashed_own_gpg_home_recovered")
        self.check(f"{engine}.real_process_crash_recovery")

        local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        scheduled_time = (local_now - timedelta(minutes=1)).strftime("%H:%M")
        api.configure(schedule_enabled=True, schedule_time=scheduled_time)
        overview = api.request("GET", BACKUPS)
        next_due = datetime.fromisoformat(overview["next_scheduled_at"].replace("Z", "+00:00"))
        require(next_due > datetime.now(UTC), "changed_plan_future_slot")
        require(next_due.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M") == scheduled_time, "beijing_slot")
        before = api.request("GET", f"{BACKUPS}/runs?source=scheduled")["total"]
        command("docker", "stop", "--time", "15", container, check="stop_owned_app_for_offline_plan_fixture")
        require(self.lab.maintenance(engine, "schedule_due")["due_fixture_written"], "offline_due_fixture")
        command("docker", "start", container, check="restart_owned_app_with_overdue_plan")
        api.ready(self.lab.app_url(container))
        until = time.monotonic() + 120
        while time.monotonic() < until:
            rows = api.request("GET", f"{BACKUPS}/runs?source=scheduled")
            if rows["total"] > before:
                auto = api.wait(rows["items"][0]["id"])
                break
            time.sleep(0.2)
        else:
            raise AcceptanceFailure("overdue_schedule_runs_after_restart")
        require(auto["source"] == "scheduled" and auto["status"] == "succeeded", "real_scheduled_backup_succeeded")
        require(rows["total"] == before + 1, "offline_days_not_replayed")
        command("docker", "restart", "--time", "15", container, check="restart_owned_app_for_plan_dedup")
        api.ready(self.lab.app_url(container))
        time.sleep(1.2)
        require(api.request("GET", f"{BACKUPS}/runs?source=scheduled")["total"] == before + 1, "same_slot_not_repeated")
        api.configure(schedule_enabled=False)
        terminal = api.run()
        require(terminal["status"] == "succeeded", "manual_works_when_schedule_off")
        self.check(f"{engine}.beijing_plan_offline_latest_slot_dedup_and_manual_switch")
        command("docker", "stop", "--time", "15", container, check="stop_owned_app_for_terminal_cleanup_fixture")
        fixture = self.lab.maintenance(engine, "terminal_work_fixture", {"run_id": terminal["id"]})
        require(fixture["terminal_failure_fixture_written"], "terminal_cleanup_failure_fixture")
        command("docker", "start", container, check="start_owned_app_after_terminal_cleanup_fixture")
        api.ready(self.lab.app_url(container))
        verified = self.lab.probe(container, "terminal_work_verify", {"run_id": terminal["id"]})
        require(all(verified.values()), "terminal_plaintext_cleanup_exact_identity_and_failure_history")
        self.check(f"{engine}.terminal_cleanup_retry_preserves_unknown_files_and_failure_history")

    def execute(self, scenario: str) -> None:
        self.lab.start_postgres()
        self.lab.start_external_services()
        for engine in ("postgresql", "sqlite"):
            container, url = self.lab.start_app(engine)
            api = self.client(url)
            api.ready()
            api.login("acceptance_admin", self.lab.admin_password)
            if engine == "postgresql":
                self.lab.facts["tools"] = self.lab.probe(container, "tools")
                require(" 16." in self.lab.facts["tools"]["pg_dump"], "postgresql_16_client_installed")
            paths = api.request("GET", "/openapi.json")["paths"]
            require(not any(path.startswith(BACKUPS) and "restore" in path for path in paths), "no_http_restore_api")
            self.seed(api, url, engine)
            self.permission_matrix(api, url, engine)
            self.configure_initial(api, engine)
            if engine == "postgresql":
                until = time.monotonic() + 90
                while time.monotonic() < until:
                    try:
                        self.lab.probe(container, "s3", {
                            "operation": "create", "buckets": ["acceptance-backups", "acceptance-secondary"],
                            "access_key": self.lab.s3_access, "secret_key": self.lab.s3_secret,
                        })
                        break
                    except AcceptanceFailure:
                        time.sleep(0.5)
                else:
                    raise AcceptanceFailure("tls_s3_ready_and_versioning_enabled")
            if scenario in ("all", "roundtrip"):
                self.roundtrip(api, container, engine)
            if scenario in ("all", "storage"):
                self.storage(api, container, engine)
            if scenario in ("all", "smtp"):
                self.smtp_failure(api, engine)
            if scenario in ("all", "reliability"):
                self.idempotency_restart(api, container, engine)
            require(all(self.lab.probe(container, "secret_storage", {
                "secrets": self.expected_secrets(),
            }).values()), "settings_and_artifact_credentials_encrypted_terminal_snapshots_cleared")
            self.check(f"{engine}.database_secret_ciphertext_and_terminal_snapshot_cleanup")
        self.lab.assert_logs_safe()
        self.check("container_logs_no_test_secrets")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all", "roundtrip", "storage", "smtp", "reliability"), default="all")
    parser.add_argument("--image", default="ainovel-backup-acceptance:local")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.output.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    lab = DockerLab(arguments.image)
    acceptance = Acceptance(lab)
    failure = None
    try:
        with lab:
            try:
                acceptance.execute(arguments.scenario)
            finally:
                acceptance.close()
    except AcceptanceFailure as error:
        failure = str(error)
    except Exception as error:
        # 类型足够定位 harness 问题；原异常可能含 URL、SQL 或测试密码。
        failure = f"unexpected_{type(error).__name__}"
    result = {
        "scenario": arguments.scenario, "status": "failed" if failure else "passed",
        "started_at": started.isoformat(), "finished_at": datetime.now(UTC).isoformat(),
        "checks": acceptance.checks, "failure_check": failure, "environment": lab.facts,
        "excluded": ["production_data", "external_email_delivery", "browser_checks"],
    }
    (arguments.output / f"{arguments.scenario}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    with (arguments.output / "history.jsonl").open("a", encoding="utf-8") as history:
        history.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(f"{'FAIL ' + failure if failure else 'PASS acceptance'}", flush=True)
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
