"""自定义切章正则的子进程入口（spawn 方式启动，只依赖标准库，避免子进程导入整个应用）。"""

import re
from typing import Any


def find_matches(pattern: str, lines: list[tuple[int, str]]) -> list[int]:
    """返回匹配到的行号（lines 为 (行号, 去掉首尾空白的行文本)）。"""
    rx = re.compile(pattern)
    return [i for i, line in lines if rx.search(line)]


def run(pattern: str, lines: list[tuple[int, str]], conn: Any) -> None:
    """子进程主函数：结果经管道发回 ("ok", 行号列表) / ("error", 说明)。"""
    try:
        conn.send(("ok", find_matches(pattern, lines)))
    except Exception as exc:  # noqa: BLE001
        conn.send(("error", str(exc)[:200]))
    finally:
        conn.close()
