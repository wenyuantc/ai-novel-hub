"""手动验收双实例 Compose：仅使用本脚本生成的临时资源。

先构建本次检出的镜像，再执行：
    python tests/c7_container_smoke.py --image <本次镜像标签>
该脚本不会读取项目 .env，不作为默认 pytest 测试。
"""

import argparse
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from c7_container_diagnostics import DEFAULT_REPORT_DIR, Diagnostics, ProbeFailure, checked_run, wait_ready

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.multi.yml"
PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")


def run(args, *, env, timeout=120, check=True):
    return checked_run(args, cwd=ROOT, env=env, timeout=timeout, check=check)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_workers(compose, env, count):
    sql = (
        "SELECT hostname,pid,peer_url,count(*) FROM task_workers "
        "WHERE stopped_at IS NULL AND fenced_at IS NULL AND peer_url IS NOT NULL "
        "AND heartbeat_at >= CURRENT_TIMESTAMP - interval '30 seconds' "
        "GROUP BY hostname,pid,peer_url ORDER BY peer_url,pid"
    )
    deadline = time.monotonic() + 90
    last = ""
    while time.monotonic() < deadline:
        last = run(
            [*compose, "exec", "-T", "db", "psql", "-U", "c7_probe", "-d", "c7_probe", "-At", "-F", "|", "-c", sql],
            env=env,
            check=False,
        )
        rows = [row.split("|") for row in last.splitlines() if row.count("|") == 3]
        grouped = {name: [] for name in ("app1", "app2")}
        for hostname, pid, url, registrations in rows:
            peer = urlsplit(url)
            if peer.scheme == "tcp" and peer.hostname in grouped and peer.port:
                grouped[peer.hostname].append((hostname, int(pid), peer.port, int(registrations)))
        if all(len(grouped[name]) == count and all(row[3] == 2 for row in grouped[name]) for name in grouped):
            if all(
                len({(row[0], row[1]) for row in grouped[name]}) == count
                and len({row[2] for row in grouped[name]}) == count
                for name in grouped
            ):
                return grouped
        time.sleep(0.25)
    raise ProbeFailure("worker_registration")


def verify_peer_links(compose, env, grouped):
    code = (
        "import socket,sys; "
        "a=socket.getaddrinfo(sys.argv[1],0,type=socket.SOCK_STREAM)[0][4][0]; "
        "s=socket.create_connection((sys.argv[1],int(sys.argv[2])),timeout=5); "
        "s.close(); print(a)"
    )
    for source, target in (("app1", "app2"), ("app2", "app1")):
        for _, _, port, _ in grouped[target]:
            address = run([*compose, "exec", "-T", source, "python", "-c", code, target, str(port)], env=env)
            print(f"{source} → {target}:{port} 可达，DNS={address}")


def verify_exposure(compose, env, grouped):
    targets = []
    for name in ("app1", "app2"):
        container_id = run([*compose, "ps", "-q", name], env=env)
        inspected = json.loads(run(["docker", "inspect", container_id], env=env))[0]
        ports = inspected["NetworkSettings"]["Ports"]
        if not ports.get("8000/tcp"):
            raise ProbeFailure("port_exposure")
        networks = inspected["NetworkSettings"]["Networks"]
        addresses = [network["IPAddress"] for network in networks.values() if network.get("IPAddress")]
        if len(addresses) != 1:
            raise ProbeFailure("external_network_unavailable")
        for _, _, peer_port, _ in grouped[name]:
            if ports.get(f"{peer_port}/tcp"):
                raise ProbeFailure("port_exposure")
            targets.append((addresses[0], peer_port))
    return targets


def require_external_isolation(results):
    if not isinstance(results, list) or not results or any(
        not isinstance(item, dict) or type(item.get("connected")) is not bool for item in results
    ):
        raise ProbeFailure("external_network_unavailable")
    if any(item["connected"] for item in results):
        raise ProbeFailure("external_network_reachable")


def validate_external_results(payload, count):
    if (not isinstance(payload, dict) or payload.get("positive_control") is not True
            or not isinstance(payload.get("targets"), list) or len(payload["targets"]) != count):
        raise ProbeFailure("external_network_unavailable")
    return payload["targets"]


def verify_external_connections(env, targets, image, project, diagnostic, trusted_recheck):
    """在另一 Docker bridge 实测，宿主机结果单独报告且不视为公网。"""
    diagnostic.set_stage("external_network")
    name = f"{project}-{diagnostic.scenario}-outside"
    label = f"ainovel.c7.external={project}"
    run(["docker", "network", "create", "--label", label, name], env=env)
    control_code = (
        "import socket\n"
        "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
        "s.bind(('0.0.0.0',45678)); s.listen(1); s.settimeout(90)\n"
        "c,_=s.accept(); c.close(); s.close()\n"
    )
    control = run([
        "docker", "run", "-d", "--name", f"{name}-control", "--label", label, "--network", name,
        "--network-alias", "c7-positive-control", "--entrypoint", "python", image, "-c", control_code,
    ], env=env)
    code = (
        "import json,socket,sys,time\n"
        "control=False\n"
        "for _ in range(50):\n"
        " try:\n"
        "  s=socket.create_connection(('c7-positive-control',45678),timeout=1); s.close(); control=True; break\n"
        " except OSError: time.sleep(0.1)\n"
        "result=[]\n"
        "for host,port in json.loads(sys.argv[1]):\n"
        " s=socket.socket(); s.settimeout(1)\n"
        " try:\n"
        "  s.connect((host,port)); result.append({'connected':True})\n"
        " except OSError:\n"
        "  result.append({'connected':False})\n"
        " finally: s.close()\n"
        "print(json.dumps({'positive_control':control,'targets':result}))\n"
    )
    container = run([
        "docker", "create", "--name", f"{name}-probe", "--label", label, "--network", name,
        "--entrypoint", "python", image, "-c", code, json.dumps(targets),
    ], env=env)
    run(["docker", "start", "-a", container], env=env, timeout=120)
    for member in (control, container):
        inspected = json.loads(run(["docker", "inspect", member], env=env))[0]
        if set(inspected["NetworkSettings"]["Networks"]) != {name}:
            raise ProbeFailure("external_network_unavailable")
    results = validate_external_results(json.loads(run(["docker", "logs", container], env=env)), len(targets))
    diagnostic.set_stage("peer_connectivity")
    trusted_recheck()
    diagnostic.set_stage("external_network")
    require_external_isolation(results)
    host_connected = 0
    for address, port in targets:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            try:
                host_connected += sock.connect_ex((address, port)) == 0
            except OSError:
                pass
    print(f"独立 Docker 网络直连 {len(targets)} 个 peer 端口均失败；"
          f"宿主机可直连 {host_connected}/{len(targets)}，仅代表本机网络环境")


def cleanup_owned(compose, project, image_tags, env, started):
    errors = False
    if started:
        try:
            run([*compose, "down", "--volumes", "--remove-orphans"], env=env, timeout=180)
        except (OSError, ProbeFailure, subprocess.TimeoutExpired):
            errors = True
    label = f"label=ainovel.c7.external={project}"
    for _ in range(10):
        try:
            containers = run(["docker", "ps", "-aq", "--filter", label], env=env).splitlines()
            for container in containers:
                run(["docker", "rm", "-f", container], env=env)
            networks = run(["docker", "network", "ls", "-q", "--filter", label], env=env).splitlines()
            for network in networks:
                run(["docker", "network", "rm", network], env=env)
            if not run(["docker", "ps", "-aq", "--filter", label], env=env) and not run(
                ["docker", "network", "ls", "-q", "--filter", label], env=env,
            ):
                time.sleep(0.25)
                if not run(["docker", "ps", "-aq", "--filter", label], env=env):
                    break
        except (OSError, ProbeFailure, subprocess.TimeoutExpired):
            pass
        time.sleep(0.25)
    else:
        errors = True
    for tag in image_tags:
        try:
            if run(["docker", "image", "ls", "-q", "--filter", f"reference={tag}"], env=env):
                run(["docker", "image", "rm", tag], env=env)
        except (OSError, ProbeFailure, subprocess.TimeoutExpired):
            errors = True
    project_label = f"label=com.docker.compose.project={project}"
    try:
        for kind, extra in (("ps", ["-aq"]), ("network", ["ls", "-q"]), ("volume", ["ls", "-q"])):
            if run(["docker", kind, *extra, "--filter", project_label], env=env):
                errors = True
        for tag in image_tags:
            if run(["docker", "image", "ls", "-q", "--filter", f"reference={tag}"], env=env):
                errors = True
    except (OSError, ProbeFailure, subprocess.TimeoutExpired):
        errors = True
    if errors:
        raise ProbeFailure("cleanup_unconfirmed")


def capture_exits(compose, env, diagnostic):
    for index, name in enumerate(("app1", "app2", "db")):
        try:
            container = run([*compose, "ps", "--all", "--quiet", name], env=env, check=False)
            if not container:
                continue
            value = run(["docker", "inspect", "--format", "{{.State.Status}}|{{.State.ExitCode}}", container],
                        env=env, check=False)
            state, code = value.split("|", 1)
            diagnostic.record_exit(index, int(code) if state in {"exited", "dead"} else None, state)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue


def execute(args, diagnostic):
    env = {key: os.environ[key] for key in PASSTHROUGH if key in os.environ}
    project = "c7deploy" + secrets.token_hex(8)
    image_tags = [f"{project}-app1", f"{project}-app2"]
    for kind, extra in (("ps", ["-aq"]), ("network", ["ls", "-q"]), ("volume", ["ls", "-q"])):
        existing = run(["docker", kind, *extra, "--filter", f"label=com.docker.compose.project={project}"], env=env)
        if existing:
            raise ProbeFailure("invalid_state")
    for kind, extra in (("ps", ["-aq"]), ("network", ["ls", "-q"])):
        if run(["docker", kind, *extra, "--filter", f"label=ainovel.c7.external={project}"], env=env):
            raise ProbeFailure("invalid_state")
    for tag in image_tags:
        if run(["docker", "image", "ls", "-q", "--filter", f"reference={tag}"], env=env):
            raise ProbeFailure("invalid_state")

    with tempfile.TemporaryDirectory(prefix="ainovel-c7-deploy-") as temporary:
        directory = Path(temporary)
        (directory / "backups").mkdir()
        (directory / "media").mkdir()
        port1, port2 = free_port(), free_port()
        while port2 == port1:
            port2 = free_port()
        secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        password = secrets.token_hex(16)
        signing_key = secrets.token_hex(32)
        env_path = directory / "isolated.env"
        env_path.write_text(
            "POSTGRES_USER=c7_probe\nPOSTGRES_DB=c7_probe\n"
            f"POSTGRES_PASSWORD={password}\n"
            f"JWT_SECRET={signing_key}\nENCRYPTION_KEY={secret}\n"
            f"APP_PORT=127.0.0.1:{port1}\nAPP2_PORT=127.0.0.1:{port2}\n"
            f"BACKUP_DIR={directory / 'backups'}\n"
            f"MEDIA_DIR={directory / 'media'}\n"
        )
        env_path.chmod(0o600)
        compose = ["docker", "compose", "--env-file", str(env_path), "-p", project, "-f", str(COMPOSE)]
        tagged = []
        started = False
        try:
            diagnostic.set_scenario("compose")
            diagnostic.set_stage("image_tag")
            for tag in image_tags:
                tagged.append(tag)
                run(["docker", "image", "tag", args.image, tag], env=env)
            diagnostic.set_stage("compose_config")
            run([*compose, "config", "--quiet"], env=env)
            started = True
            diagnostic.set_stage("compose_start")
            run([*compose, "up", "-d", "--no-build"], env=env, timeout=180)
            diagnostic.set_stage("services")
            services = run([*compose, "ps", "--all", "--services"], env=env).splitlines()
            if set(services) != {"db", "app1", "app2"}:
                raise ProbeFailure("invalid_state")
            diagnostic.set_scenario("default_workers")
            diagnostic.set_stage("readiness")
            wait_ready((port1, port2), diagnostic)
            diagnostic.set_stage("worker_registration")
            workers = wait_workers(compose, env, 1)
            diagnostic.set_stage("port_exposure")
            targets = verify_exposure(compose, env, workers)
            diagnostic.set_stage("peer_connectivity")
            verify_peer_links(compose, env, workers)
            verify_external_connections(
                env, targets, args.image, project, diagnostic, lambda: verify_peer_links(compose, env, workers),
            )
            print("默认配置：两个实例各 1 个 worker，且各有两份有效登记")

            diagnostic.set_scenario("four_workers")
            diagnostic.set_stage("recreate")
            with env_path.open("a") as config:
                config.write("UVICORN_WORKERS=4\n")
            run([*compose, "up", "-d", "--no-build", "--force-recreate", "--no-deps", "app1", "app2"],
                env=env, timeout=180)
            diagnostic.set_stage("readiness")
            wait_ready((port1, port2), diagnostic)
            diagnostic.set_stage("worker_registration")
            workers = wait_workers(compose, env, 4)
            diagnostic.set_stage("port_exposure")
            targets = verify_exposure(compose, env, workers)
            diagnostic.set_stage("peer_connectivity")
            verify_peer_links(compose, env, workers)
            verify_external_connections(
                env, targets, args.image, project, diagnostic, lambda: verify_peer_links(compose, env, workers),
            )
            print("UVICORN_WORKERS=4：两个实例各 4 个 worker，且各有两份有效登记")
        except BaseException as exc:
            if isinstance(exc, subprocess.TimeoutExpired) or (
                diagnostic.stage in {"image_tag", "compose_start", "recreate", "external_network"}
                and (isinstance(exc, (OSError, KeyboardInterrupt))
                     or isinstance(exc, ProbeFailure) and exc.category == "command_failed")
            ):
                # CLI 出错后 daemon 可能仍在处理请求；即使当前扫描为空也不能确认最终清理。
                diagnostic.cleanup_uncertain = True
            if started:
                capture_exits(compose, env, diagnostic)
            raise
        finally:
            had_error = sys.exc_info()[0] is not None
            try:
                cleanup_owned(compose, project, tagged, env, started)
                diagnostic.cleanup_confirmed = not diagnostic.cleanup_uncertain
            except (OSError, ProbeFailure, subprocess.TimeoutExpired):
                diagnostic.cleanup_confirmed = False
                if not had_error:
                    diagnostic.set_stage("cleanup")
                    raise ProbeFailure("cleanup_unconfirmed") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="本次检出的已构建镜像")
    parser.add_argument("--diagnostics-dir", type=Path, default=DEFAULT_REPORT_DIR, help="失败诊断目录")
    args = parser.parse_args()
    diagnostic = Diagnostics("compose", args.diagnostics_dir)
    try:
        execute(args, diagnostic)
    except BaseException as exc:
        try:
            report = diagnostic.persist(exc)
            print(f"容器验收失败，结构化诊断：{report}", file=sys.stderr)
        except OSError:
            print("容器验收失败，无法保存结构化诊断", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
