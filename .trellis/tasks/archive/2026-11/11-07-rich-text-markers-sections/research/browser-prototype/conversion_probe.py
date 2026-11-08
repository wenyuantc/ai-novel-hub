"""真实 Chromium 原型创建区块，直接将其结构化结果送入后端转换原语。

此脚本只覆盖当前原型的纯段落/单区块，并非正式前端适配器或端到端产品验收。
需先按 README 启动隔离 Vite 与 richproof 浏览器会话。
"""

import json
import subprocess
from uuid import UUID

from app.schemas.rich_document import SceneDocument
from app.services.rich_document_conversion import conversion

KEY = "0981fe8e-f4cc-4336-80a2-df11c2812834"
PLAYWRIGHT = "/Users/wenyuan/.codex/skills/playwright/scripts/playwright_cli.sh"
code = (
    "async (page) => await page.evaluate(() => {poc.load('甲乙'); "
    "poc.editor.commands.setTextSelection(2); poc.insertSection('" + KEY + "'); return poc.state();})"
)
output = subprocess.run([PLAYWRIGHT, "-s=richproof", "--raw", "run-code", code],
                        check=True, text=True, capture_output=True).stdout
state = json.loads(output)
assert state["text"] == "甲乙"


def text(node):
    return node.get("text", "") + "".join(text(child) for child in node.get("content", []))


formal = ""
boundaries = []
sections = []
last_type = None
for node in state["json"]["content"]:
    if node["type"] == "paragraph":
        prefix = node["attrs"]["prefix"]
        if last_type == "section" and prefix == "":
            boundaries.append(len(formal))
        formal += prefix + text(node)
    elif node["type"] == "section":
        sections.append({"key": node["attrs"]["id"], "anchor": len(formal), "order": 0,
                         "title": "原型区块", "content": text(node), "include_in_ai": False})
    else:
        raise AssertionError(f"不支持的原型节点 {node['type']}")
    last_type = node["type"]
assert formal == "甲乙" and boundaries == [1] and sections[0]["content"] == "备选"
doc = SceneDocument.model_validate({"content": formal, "formatting": {"version": 1, "boundaries": boundaries},
                                    "sections": sections, "markers": []})
converted = conversion(doc, UUID(KEY))
assert converted.content == "甲备选乙" and converted.formatting.boundaries == []
print("PASS: 浏览器创建区块 → 原型结构化边界 → 后端转换，正文甲备选乙，未插入换行")
