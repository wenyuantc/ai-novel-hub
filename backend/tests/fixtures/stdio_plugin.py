"""本机插件验收用的离线 MCP 示例；只提供一个记录调用的固定工具。"""

import json
import os
import subprocess
import sys
import time


def record(event):
    if len(sys.argv) > 1:
        with open(sys.argv[1], "a", encoding="utf-8") as history:
            history.write(json.dumps({"event": event, "pid": os.getpid()}) + "\n")


record("start")
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if "id" not in msg:
        continue
    if method == "initialize":
        result = {
            "protocolVersion": msg["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [{"name": "record", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        record("call")
        if msg["params"].get("arguments", {}).get("disconnect"):
            sys.exit(0)
        if msg["params"].get("arguments", {}).get("wait"):
            time.sleep(120)
        detail = {"pid": os.getpid(), "child": child.pid, "uid": os.geteuid(), "marker": os.getenv("WORKER_MARKER")}
        result = {"content": [{"type": "text", "text": json.dumps(detail)}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
