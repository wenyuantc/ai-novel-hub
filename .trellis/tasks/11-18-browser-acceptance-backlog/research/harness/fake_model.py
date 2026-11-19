"""浏览器验收用的本地假模型：OpenAI 兼容接口，只绑定 127.0.0.1，只用标准库。

用法：python fake_model.py --port <端口> --dir <临时目录>

模型接口
- POST /v1/chat/completions：流式（SSE）与非流式，均带 usage；带 tools 的请求视为智能体对话。
- GET  /v1/models：模型配置的「获取模型列表 / 测试连接」会用到。
- POST /v1/embeddings：确定性的假向量（向量模型配置时可用）。

默认按提示词里锁定的输出结构（backend/app/ai/prompts/formats.py）分流，见 classify()。
所有请求体追加写入 <dir>/requests.jsonl。

控制接口（仅回环）
- POST   /_control/script   压入一次性脚本，见 Script 说明；body 可为单个对象或数组
- GET    /_control/script   查看尚未消费的脚本
- DELETE /_control/script   清空脚本
- POST   /_control/delay    {"first_byte": 秒, "per_chunk": 秒, "kinds": ["extract"]|null}；全 0 即关闭
- GET    /_control/calls    ?kind=&since=<序号> 查询调用记录（不含完整请求体，完整内容看 requests.jsonl）
- DELETE /_control/calls    清空内存中的调用记录（requests.jsonl 不动）
- GET    /_control/state    当前延迟、脚本数、调用计数

Script（一次性，按压入顺序匹配第一条 match 符合的脚本）
- match：  "tools"（带工具的请求，默认）｜"text"（不带工具）｜"any"｜某个 kind 名（如 "extract"）
- type：   "text"（{"text": "..."}）｜"tool_calls"（{"tool_calls": [{"name": ..., "arguments": {...}|"..."}], "text": 可选前导文本}）
           ｜"error"（{"status": 503, "message": "..."}）
- times：  可选，命中多少次后才移除（默认 1）
- delay：  可选，{"first_byte": 秒, "per_chunk": 秒}，只作用于这条脚本
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.Lock()
STATE: dict = {
    "dir": ".",
    "scripts": [],
    "delay": {"first_byte": 0.0, "per_chunk": 0.0, "kinds": None},
    "calls": [],
    "seq": 0,
}

# ---------------- 分流 ----------------
# 依据：backend/app/ai/prompts/formats.py 中锁定的输出结构文本，模板整体引用它们，因此可作为可靠标识。
# 顺序有意义：更具体的标识在前（例如角色生成的结构里也含 "target"）。
MARKERS: list[tuple[str, str]] = [
    ('"summary": "本章摘要', "analysis"),                       # ANALYSIS_SCHEMA（含伏笔引用变体）
    ('"facts": [{"category"', "extract"),                       # CONSISTENCY_EXTRACT_SCHEMA
    ('"issues": [{"existing_issue_id"', "compare"),             # CONSISTENCY_COMPARE_SCHEMA
    ('"changes": [{"kind": "added', "impact"),                  # CONSISTENCY_IMPACT_SCHEMA
    ('"project": {"synopsis"', "book_synthesize"),              # BOOK_SYNTHESIZE_FORMAT
    ('"chapters": [{"chapter_no": 12', "book_digest"),          # BOOK_DIGEST_FORMAT
    ('"volumes": [', "wizard_outline"),                         # OUTLINE_FORMAT
    ('"key_events"', "outline_expand"),                         # OUTLINE_EXPAND_FORMAT
    ('"volume_title": "（仅开新卷', "outline_continue"),         # OUTLINE_CONTINUE_FORMAT
    ('"character": {"name": "角色名"', "character_generate"),    # character_generate_format
    ('"organization": {"name": "组织名"', "organization_generate"),  # ORGANIZATION_FORMAT
    ('"a_id": 11', "relations_generate"),                       # RELATIONS_GENERATE_FORMAT
    ('"relationships": [\n    {"target"', "relations_parse"),   # RELATIONS_PARSE_FORMAT
    ('"characters": [\n    {"name": "角色名"', "wizard_characters"),  # characters_wizard_format
    ('"careers": [\n    {"name": "剑修"', "careers"),            # CAREER_FORMAT（向导与职业生成共用）
    ('{"title": "条目名称", "category": "place"', "wizard_world"),  # WORLD_FORMAT
    ('"description_density"', "style_extract"),                 # STYLE_EXTRACT_FORMAT
    ('"options": ["选项1"', "inspiration"),                      # INSPIRATION_FORMAT
    ("将小说项目助手的对话历史压缩为简洁的中文摘要", "agent_summary"),  # agent.summarize.system.j2
]


def message_text(messages: list) -> str:
    parts = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return "\n".join(parts)


def last_user(messages: list) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return message_text(messages)


def classify(body: dict, text: str) -> str:
    if body.get("tools"):
        return "agent"
    for marker, kind in MARKERS:
        if marker in text:
            return kind
    return "text"


# ---------------- 一致性：抽取 / 比对 / 影响 ----------------
ITEM = re.compile(r"佩剑|玉佩|法器\d+")
PERSON = re.compile(r"老周|阿青")
ITEM_GONE = re.compile(r"断|毁|碎|丢失|遗失")
ITEM_USE = r"(拔出|取出|握着|挥动|握紧)"


def extract(user: str) -> dict:
    """依据 consistency.extract.user.j2：正文位于【正文】与【事实类别】之间；quote 必须逐字摘自正文。"""
    body = user.split("【正文】", 1)[-1].split("【事实类别】", 1)[0]
    body = body.split("\n", 1)[1] if "\n" in body else body  # 去掉章节标题行
    facts = []
    for raw in re.split(r"(?<=[。！？\n])", body):
        sentence = raw.strip()
        if not sentence:
            continue
        for subject in dict.fromkeys(ITEM.findall(sentence)):
            if ITEM_GONE.search(sentence):
                facts.append({"category": "item", "subject": subject, "statement": f"{subject}已毁",
                              "time_hint": "", "quote": sentence})
            elif re.search(ITEM_USE + subject, sentence):
                facts.append({"category": "item", "subject": subject, "statement": f"{subject}仍在使用",
                              "time_hint": "", "quote": sentence})
            elif "完好" in sentence or "收好" in sentence or "佩在" in sentence:
                facts.append({"category": "item", "subject": subject, "statement": f"{subject}完好无损",
                              "time_hint": "", "quote": sentence})
        for subject in dict.fromkeys(PERSON.findall(sentence)):
            if "身亡" in sentence:
                facts.append({"category": "character_state", "subject": subject, "statement": f"{subject}已身亡",
                              "time_hint": "", "quote": sentence})
            elif "现身" in sentence:
                facts.append({"category": "character_state", "subject": subject, "statement": f"{subject}仍然在世",
                              "time_hint": "", "quote": sentence})
    return {"facts": facts}


# issues.fact_line：[chapter_id=X fact_id=Y] 第N章｜类别｜主体｜陈述｜时间线索
FACT_LINE = re.compile(r"^\[chapter_id=(\d+) fact_id=([^\]]+)\] 第(\d+)章｜([^｜]*)｜([^｜]*)｜([^｜]*)｜?(.*)$")
EXISTING_LINE = re.compile(r"^#(\d+) ([^｜]*)｜([^｜]*)｜")


def compare(user: str) -> dict:
    """依据 consistency.compare.user.j2 与 issues.parse_comparison：证据只能引用台账里的 chapter_id + fact_id。"""
    facts_part = user.split("【事实台账】", 1)[-1]
    existing_part = ""
    if "【已有问题】" in facts_part:
        existing_part = facts_part.split("【已有问题】", 1)[1].split("【输出 JSON 结构】", 1)[0]
    facts_part = re.split(r"【伏笔记录】|【已有问题】|【输出 JSON 结构】", facts_part, maxsplit=1)[0]
    by_subject: dict[str, list[dict]] = {}
    for line in facts_part.splitlines():
        m = FACT_LINE.match(line.strip())
        if m:
            by_subject.setdefault(m.group(5), []).append(
                {"chapter_id": int(m.group(1)), "fact_id": m.group(2), "no": int(m.group(3)),
                 "statement": m.group(6)})
    existing = {}
    for line in existing_part.splitlines():
        m = EXISTING_LINE.match(line.strip())
        if m:
            existing[m.group(3)] = int(m.group(1))
    issues = []
    for subject, facts in by_subject.items():
        gone = [f for f in facts if "已毁" in f["statement"] or "已身亡" in f["statement"]]
        if not gone:
            continue
        first = min(gone, key=lambda f: f["no"])
        later = [f for f in facts if ("仍在使用" in f["statement"] or "仍然在世" in f["statement"])
                 and f["no"] > first["no"]]
        if not later:
            continue
        second = min(later, key=lambda f: f["no"])
        person = "已身亡" in first["statement"]
        title = f"潜在冲突：{subject}" + ("已身亡却再次现身" if person else "已毁却仍在使用")
        issues.append({
            "existing_issue_id": existing.get(title),
            "category": "character_state" if person else "item",
            "title": title,
            "description": f"第{first['no']}章写明{first['statement']}，第{second['no']}章却写{second['statement']}，"
                           "两处可能无法同时成立。",
            "suggestion": f"请核对第{second['no']}章，补充{subject}的来历或改写该处描写。",
            "severity": "high",
            "foreshadow_id": None,
            "evidence": [{"chapter_id": first["chapter_id"], "fact_id": first["fact_id"]},
                         {"chapter_id": second["chapter_id"], "fact_id": second["fact_id"]}],
        })
    return {"issues": issues}


# impact_task._fact_lines：[A1] 类别｜主体｜陈述｜时间线索（A* 改动前，B* 改动后）
IMPACT_LINE = re.compile(r"^\[([AB]\d+)\] ([^｜]*)｜([^｜]*)｜([^｜]*)｜?(.*)$")


def impact(user: str) -> dict:
    """依据 consistency.impact.user.j2 与 impact_task.parse_changes：同主体配成 changed，其余 removed/added。"""
    before, after = [], []
    for line in user.splitlines():
        m = IMPACT_LINE.match(line.strip())
        if m:
            (before if m.group(1).startswith("A") else after).append((m.group(1), m.group(3)))
    changes, used_after = [], set()
    for bid, subject in before:
        match = next((aid for aid, s in after if s == subject and aid not in used_after), None)
        if match:
            used_after.add(match)
            changes.append({"kind": "changed", "before_id": bid, "after_id": match})
        else:
            changes.append({"kind": "removed", "before_id": bid, "after_id": None})
    for aid, _ in after:
        if aid not in used_after:
            changes.append({"kind": "added", "before_id": None, "after_id": aid})
    return {"changes": changes}


# ---------------- 章节分析 ----------------


def analysis(text: str) -> dict:
    """结构对照 tests/helpers.py::analysis_json 与 ANALYSIS_SCHEMA。

    出现「林远」时给出一条角色状态变化（回写清单 → 设定变化），情节点与钩子用于生成剧情记忆。
    """
    gone = "佩剑" in text and bool(ITEM_GONE.search(text.split("佩剑", 1)[1][:40]))
    changes = []
    if "林远" in text:
        changes.append({"name": "林远", "current_state": "佩剑已毁，赤手下山" if gone else "继续赶路"})
    return {
        "summary": ("林远在山谷一战中力竭，佩剑被巨石砸断，只得赤手下山。" if gone
                    else "林远沿官道继续赶路，途中见闻平常，为后续情节铺垫。"),
        "plot_points": [{"text": "林远的佩剑在山谷中被毁" if gone else "林远继续赶路", "importance": 4},
                        {"text": "林远赤手下山" if gone else "途中风平浪静", "importance": 3}],
        "hooks": [{"type": "悬念", "strength": 7, "text": "失去佩剑后林远如何应敌" if gone else "前路未知"}],
        "conflict": {"level": 6, "types": ["人与环境"]},
        "emotion": {"tone": "沉郁", "start": "紧张", "middle": "激烈", "end": "低落"},
        "character_changes": changes,
        "relationship_changes": [],
        "career_changes": [],
        "organization_changes": [],
        "foreshadows": [],
        "scores": {"pacing": 7, "engagement": 7, "coherence": 8},
        "suggestions": [
            {"category": "节奏", "priority": "high", "text": "山谷一战的铺垫可以再紧凑一些"},
            {"category": "描写", "priority": "medium", "text": "补充佩剑断裂瞬间的细节"},
            {"category": "人物", "priority": "low", "text": "交代林远下山时的心理变化"},
        ],
    }


# ---------------- 其他 JSON 任务（照 formats.py 的结构给出合法样例） ----------------
STATIC: dict[str, object] = {
    "book_synthesize": {
        "project": {"synopsis": "少年剑客林远踏入江湖的成长故事。", "theme": "成长", "tags": ["武侠"],
                    "selling_points": "剑与江湖", "audience": "武侠读者", "genre": "武侠"},
        "characters": [{"name": "林远", "aliases": [], "role_type": "主角", "profile": "少年剑客",
                        "personality": "坚毅", "appearance": "清瘦"}],
        "organizations": [], "world": [], "careers": [], "relationships": [],
    },
    "book_digest": {
        "chapters": [{"chapter_no": 1, "summary": "林远出场。"}],
        "characters": [], "organizations": [], "world": [], "careers": [], "relationships": [],
    },
    "wizard_outline": {"volumes": [{"volume_no": 1, "volume_title": "初入江湖", "chapters": [
        {"chapter_no": 1, "title": "下山", "summary": "林远离开师门。"},
        {"chapter_no": 2, "title": "官道", "summary": "林远沿官道赶路。"}]}]},
    "outline_expand": {"chapters": [{"title": "细纲一", "summary": "林远遇险。", "key_events": ["遇险"],
                                     "characters": ["林远"], "emotion": "紧张", "goal": "推进冲突",
                                     "est_words": 3000}]},
    "outline_continue": {"volume_title": "", "outlines": [{"title": "续写章", "summary": "林远继续前行。"}]},
    "character_generate": {"character": {"name": "苏晴", "aliases": [], "role_type": "配角", "age": "18",
                                         "gender": "女", "appearance": "明眸", "personality": "机敏",
                                         "background": "医家女", "profile": "随行医者"},
                           "relationships": []},
    "organization_generate": {"organization": {"name": "青云门", "org_type": "宗门", "purpose": "守护一方",
                                               "location": "青云山", "power_level": 70, "parent": "",
                                               "description": "江湖名门"},
                              "members": []},
    "relations_generate": {"relationships": []},
    "relations_parse": {"relationships": []},
    "wizard_characters": {"characters": [{"name": "林远", "aliases": [], "role_type": "主角", "age": "18",
                                          "gender": "男", "appearance": "清瘦", "personality": "坚毅",
                                          "background": "山中剑庐弟子", "profile": "少年剑客"}],
                          "relationships": []},
    "careers": {"careers": [{"name": "剑修", "kind": "main", "description": "以剑入道", "requirements": "剑心",
                             "abilities": "御剑", "rules": "逐阶突破",
                             "stages": [{"name": "练气", "description": "入门"},
                                        {"name": "筑基", "description": "根基稳固"}]}]},
    "wizard_world": [{"title": "青云山", "category": "place", "content": "林远师门所在。"}],
    "style_extract": {"name": "冷峻武侠", "description": "简洁克制", "pov": "第三人称", "rhythm": "短句为主",
                      "dialogue": "对白较少", "description_density": "中等", "tone": "冷峻", "diction": "古朴",
                      "taboos": "", "extra": ""},
    "inspiration": {"options": ["剑影长歌", "孤剑行", "山河一剑", "林远传"]},
}

CHAPTER_TEXT = (
    "暮色压在山脊上，林远沿着石阶往下走，脚步不紧不慢。山风从谷口灌进来，吹得衣角猎猎作响。\n\n"
    "他在半山腰的茶棚前停下，要了一碗粗茶。茶棚老板认出了他，笑着问他此去何方。林远只说要往南走，"
    "去看看江湖到底是什么样子。\n\n"
    "天色渐暗，远处传来几声犬吠。林远付了茶钱，把行囊往肩上一甩，继续赶路。前方的官道蜿蜒没入夜色，"
    "像一条看不见尽头的长河。"
)


def answer_for(kind: str, body: dict, text: str) -> str:
    user = last_user(body.get("messages") or [])
    if kind == "analysis":
        return json.dumps(analysis(user), ensure_ascii=False)
    if kind == "extract":
        return json.dumps(extract(user), ensure_ascii=False)
    if kind == "compare":
        return json.dumps(compare(user), ensure_ascii=False)
    if kind == "impact":
        return json.dumps(impact(user), ensure_ascii=False)
    if kind in STATIC:
        return json.dumps(STATIC[kind], ensure_ascii=False)
    if kind == "agent_summary":
        return "作者与助手讨论了作品进展，暂无未完成的操作。"
    if kind == "agent":
        return "好的，我已了解当前作品的情况。"
    return CHAPTER_TEXT


# ---------------- 脚本与延迟 ----------------


def take_script(kind: str, has_tools: bool) -> dict | None:
    with LOCK:
        for index, script in enumerate(STATE["scripts"]):
            match = script.get("match", "tools")
            if (match == "any" or match == kind or (match == "tools" and has_tools)
                    or (match == "text" and not has_tools)):
                script["_hits"] = script.get("_hits", 0) + 1
                if script["_hits"] >= int(script.get("times", 1)):
                    STATE["scripts"].pop(index)
                return dict(script)
    return None


def delays_for(kind: str, script: dict | None) -> tuple[float, float]:
    if script and isinstance(script.get("delay"), dict):
        d = script["delay"]
        return float(d.get("first_byte", 0) or 0), float(d.get("per_chunk", 0) or 0)
    with LOCK:
        d = dict(STATE["delay"])
    kinds = d.get("kinds")
    if kinds and kind not in kinds:
        return 0.0, 0.0
    return float(d.get("first_byte", 0) or 0), float(d.get("per_chunk", 0) or 0)


def record(entry: dict, body: dict) -> int:
    with LOCK:
        STATE["seq"] += 1
        entry["seq"] = STATE["seq"]
        STATE["calls"].append(entry)
        with open(os.path.join(STATE["dir"], "requests.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({**entry, "body": body}, ensure_ascii=False) + "\n")
        return entry["seq"]


def fake_vector(text: str, dim: int = 64) -> list[float]:
    vec = [0.0] * dim
    chars = [c for c in text if not c.isspace()]
    for i in range(max(1, len(chars) - 1)):
        gram = "".join(chars[i:i + 2]) or "_"
        vec[int(hashlib.md5(gram.encode()).hexdigest(), 16) % dim] += 1.0
    if not any(vec):
        vec[0] = 1.0
    return vec


# ---------------- HTTP ----------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D401 - 静默
        pass

    def _json(self, code: int, payload) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {"_raw": raw}

    def _local_only(self) -> bool:
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._json(403, {"error": "control is loopback only"})
            return False
        return True

    # --- GET ---
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/")
        if path.endswith("/models"):
            self._json(200, {"object": "list", "data": [
                {"id": "fake-model", "object": "model", "owned_by": "local"},
                {"id": "fake-embedding", "object": "model", "owned_by": "local"}]})
            return
        if not path.startswith("/_control") or not self._local_only():
            if not path.startswith("/_control"):
                self._json(404, {"error": {"message": "not found"}})
            return
        q = parse_qs(url.query)
        with LOCK:
            if path == "/_control/calls":
                since = int(q.get("since", ["0"])[0])
                kind = q.get("kind", [None])[0]
                items = [c for c in STATE["calls"] if c["seq"] > since and (kind is None or c["kind"] == kind)]
                self._json(200, {"items": items, "seq": STATE["seq"]})
            elif path == "/_control/script":
                self._json(200, {"items": STATE["scripts"]})
            elif path == "/_control/state":
                counts: dict[str, int] = {}
                for c in STATE["calls"]:
                    counts[c["kind"]] = counts.get(c["kind"], 0) + 1
                self._json(200, {"delay": STATE["delay"], "scripts": len(STATE["scripts"]),
                                 "seq": STATE["seq"], "counts": counts})
            else:
                self._json(404, {"error": "unknown control"})

    # --- DELETE ---
    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip("/")
        if not self._local_only():
            return
        with LOCK:
            if path == "/_control/calls":
                STATE["calls"].clear()
            elif path == "/_control/script":
                STATE["scripts"].clear()
            else:
                self._json(404, {"error": "unknown control"})
                return
        self._json(200, {"ok": True})

    # --- POST ---
    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        body = self._body()
        if path.startswith("/_control"):
            if not self._local_only():
                return
            if path == "/_control/script":
                items = body if isinstance(body, list) else [body]
                with LOCK:
                    STATE["scripts"].extend(items)
                    count = len(STATE["scripts"])
                self._json(200, {"ok": True, "pending": count})
            elif path == "/_control/delay":
                with LOCK:
                    STATE["delay"] = {"first_byte": float(body.get("first_byte", 0) or 0),
                                      "per_chunk": float(body.get("per_chunk", 0) or 0),
                                      "kinds": body.get("kinds")}
                    delay = dict(STATE["delay"])
                self._json(200, {"ok": True, "delay": delay})
            else:
                self._json(404, {"error": "unknown control"})
            return
        if path.endswith("/embeddings"):
            inputs = body.get("input") or []
            inputs = [inputs] if isinstance(inputs, str) else inputs
            record({"ts": time.time(), "path": path, "kind": "embedding", "answer": ""}, body)
            self._json(200, {"object": "list", "model": body.get("model", "fake-embedding"),
                             "data": [{"object": "embedding", "index": i, "embedding": fake_vector(str(t))}
                                      for i, t in enumerate(inputs)],
                             "usage": {"prompt_tokens": sum(len(str(t)) for t in inputs),
                                       "total_tokens": sum(len(str(t)) for t in inputs)}})
            return
        if not path.endswith("/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return
        self._chat(body)

    def _chat(self, body: dict) -> None:
        messages = body.get("messages") or []
        text = message_text(messages)
        has_tools = bool(body.get("tools"))
        kind = classify(body, text)
        script = take_script(kind, has_tools)
        tool_calls: list[dict] = []
        if script and script.get("type") == "error":
            answer = ""
        elif script and script.get("type") == "tool_calls":
            answer = str(script.get("text") or "")
            for i, call in enumerate(script.get("tool_calls") or []):
                args = call.get("arguments", {})
                tool_calls.append({"id": call.get("id") or f"call_{int(time.time() * 1000)}_{i}",
                                   "name": call["name"],
                                   "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)})
        elif script and script.get("type") == "text":
            answer = str(script.get("text") or "")
        else:
            answer = answer_for(kind, body, text)
        first_byte, per_chunk = delays_for(kind, script)
        seq = record({"ts": time.time(), "path": self.path, "kind": kind, "stream": bool(body.get("stream")),
                      "script": {k: v for k, v in (script or {}).items() if not k.startswith("_")} or None,
                      "answer": answer, "tool_calls": tool_calls}, body)
        if first_byte:
            time.sleep(first_byte)
        if script and script.get("type") == "error":
            status = int(script.get("status", 500))
            self._json(status, {"error": {"message": script.get("message") or f"fake failure {status}",
                                          "type": "server_error", "code": status}})
            return
        model = body.get("model", "fake-model")
        prompt_tokens = max(1, len(text) // 2)
        completion_tokens = max(1, len(answer) // 2 + 20 * len(tool_calls))
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                 "total_tokens": prompt_tokens + completion_tokens}
        finish = "tool_calls" if tool_calls else "stop"
        cid = f"chatcmpl-fake-{seq}"
        if not body.get("stream"):
            message = {"role": "assistant", "content": answer or None}
            if tool_calls:
                message["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"], "arguments": c["arguments"]}}
                                         for c in tool_calls]
            self._json(200, {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
                             "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                             "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(payload: dict) -> None:
            self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        def chunk(delta: dict, finish_reason=None) -> None:
            emit({"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]})

        try:
            chunk({"role": "assistant", "content": ""})
            step = 40
            for i in range(0, len(answer), step):
                if per_chunk:
                    time.sleep(per_chunk)
                chunk({"content": answer[i:i + step]})
            for index, call in enumerate(tool_calls):
                if per_chunk:
                    time.sleep(per_chunk)
                chunk({"tool_calls": [{"index": index, "id": call["id"], "type": "function",
                                       "function": {"name": call["name"], "arguments": ""}}]})
                args = call["arguments"]
                for i in range(0, len(args), 60):
                    chunk({"tool_calls": [{"index": index, "function": {"arguments": args[i:i + 60]}}]})
            chunk({}, finish)
            if (body.get("stream_options") or {}).get("include_usage"):
                emit({"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                      "choices": [], "usage": usage})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--dir", required=True)
    args = parser.parse_args()
    STATE["dir"] = args.dir
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
