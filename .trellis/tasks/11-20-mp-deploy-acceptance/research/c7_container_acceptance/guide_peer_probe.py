"""对文档演练的同一 Compose 项目执行 peer 正反向检查。"""

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = next(
    (parent for parent in Path(__file__).resolve().parents
     if (parent / "backend/tests/c7_container_smoke.py").is_file()),
    None,
)
if ROOT is None:
    raise RuntimeError("Cannot locate the peer probe dependencies")
sys.path.insert(0, str(ROOT / "backend/tests"))

import c7_container_smoke as smoke  # noqa: E402
from c7_container_diagnostics import Diagnostics, ProbeFailure  # noqa: E402


def cleanup_external(project, env):
    label = f"label=ainovel.c7.external={project}"
    for _ in range(12):
        try:
            containers = smoke.run(["docker", "ps", "-aq", "--filter", label], env=env).splitlines()
            for container in containers:
                smoke.run(["docker", "rm", "-f", container], env=env)
            networks = smoke.run(["docker", "network", "ls", "-q", "--filter", label], env=env).splitlines()
            for network in networks:
                smoke.run(["docker", "network", "rm", network], env=env)
            if not smoke.run(["docker", "ps", "-aq", "--filter", label], env=env) and not smoke.run(
                ["docker", "network", "ls", "-q", "--filter", label], env=env,
            ):
                return
        except (OSError, ProbeFailure, subprocess.TimeoutExpired):
            pass
        time.sleep(0.25)
    raise ProbeFailure("cleanup_unconfirmed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--compose-file", required=True)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"c7doc[0-9a-f]{16}", args.project):
        raise SystemExit("Expected the unique C7 guide project name")
    env = {key: os.environ[key] for key in smoke.PASSTHROUGH if key in os.environ}
    compose = ["docker", "compose", "--env-file", args.env_file, "-p", args.project, "-f", args.compose_file]
    diagnostic = Diagnostics("compose")
    diagnostic.set_scenario("default_workers")
    result = {"db_workers": [], "trusted_before_after": False, "positive_control": False,
              "external_blocked": False, "cleanup_confirmed": None, "error": None}
    owned = False
    uncertain = False
    stage = "preflight"
    label = f"label=ainovel.c7.external={args.project}"
    try:
        for kind, extra in (("ps", ["-aq"]), ("network", ["ls", "-q"])):
            if smoke.run(["docker", kind, *extra, "--filter", label], env=env):
                raise ProbeFailure("invalid_state")
        owned = True
        stage = "workers"
        workers = smoke.wait_workers(compose, env, 1)
        stage = "query"
        sql = (
            "SELECT DISTINCT hostname,pid,peer_url FROM task_workers "
            "WHERE stopped_at IS NULL AND fenced_at IS NULL "
            "AND heartbeat_at >= CURRENT_TIMESTAMP - INTERVAL '30 seconds' ORDER BY hostname,pid"
        )
        rows = smoke.run([*compose, "exec", "-T", "db", "psql", "-U", "c7_probe", "-d", "c7_probe",
                          "-At", "-F", "|", "-c", sql], env=env).splitlines()
        observed = set()
        for row in rows:
            host, pid, url = row.split("|", 2)
            peer = urlsplit(url)
            if peer.scheme != "tcp" or peer.hostname not in {"app1", "app2"} or not peer.port:
                raise ProbeFailure("invalid_state")
            observed.add((host, int(pid), peer.hostname, peer.port))
        expected = {(host, pid, service, port) for service, entries in workers.items()
                    for host, pid, port, _ in entries}
        if len(observed) != 2 or observed != expected:
            raise ProbeFailure("worker_registration")
        result["db_workers"] = [
            {"hostname": host, "pid": pid, "service": service, "peer_port": port}
            for host, pid, service, port in sorted(observed)
        ]
        targets = smoke.verify_exposure(compose, env, workers)
        stage = "connectivity"
        with contextlib.redirect_stdout(io.StringIO()):
            smoke.verify_peer_links(compose, env, workers)
            smoke.verify_external_connections(
                env, targets, f"{args.project}-app1", args.project, diagnostic,
                lambda: smoke.verify_peer_links(compose, env, workers),
            )
        result.update(trusted_before_after=True, positive_control=True, external_blocked=True)
    except ProbeFailure as exc:
        result["error"] = exc.category
        result["stage"] = stage
        uncertain = exc.category not in {"external_network_reachable", "worker_registration", "invalid_state"}
    except BaseException:
        result["error"] = "probe_failed"
        result["stage"] = stage
        uncertain = True
    finally:
        if owned:
            try:
                cleanup_external(args.project, env)
                result["cleanup_confirmed"] = not uncertain
            except (OSError, ProbeFailure, subprocess.TimeoutExpired):
                result["cleanup_confirmed"] = False
        else:
            result["cleanup_confirmed"] = True
    print(json.dumps(result))
    return 0 if result["error"] is None and result["cleanup_confirmed"] else 1


if __name__ == "__main__":
    sys.exit(main())
