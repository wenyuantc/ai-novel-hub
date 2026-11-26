"""按章名、正文和 Unicode 起点分页，重叠命中只在当前页展开。"""

from collections import deque
from dataclasses import dataclass, field

PAGE_SIZE = 50


@dataclass(slots=True)
class _Node:
    children: dict[str, int] = field(default_factory=dict)
    fallback: int = 0
    term: str | None = None
    count: int = 0


def _advance(nodes: list[_Node], state: int, char: str) -> int:
    while state and char not in nodes[state].children:
        state = nodes[state].fallback
    return nodes[state].children.get(char, 0)


def _matcher(terms: list[str]) -> list[_Node]:
    # 反向 Aho-Corasick：同一原文起点的所有词在同一状态的回退链上。
    nodes = [_Node()]
    for term in terms:
        state = 0
        for char in reversed(term):
            child = nodes[state].children.get(char)
            if child is None:
                child = len(nodes)
                nodes[state].children[char] = child
                nodes.append(_Node())
            state = child
        nodes[state].term = term
    queue = deque(nodes[0].children.values())
    while queue:
        node = nodes[queue.popleft()]
        node.count = int(node.term is not None) + nodes[node.fallback].count
        for char, child in node.children.items():
            nodes[child].fallback = _advance(nodes, node.fallback, char)
            queue.append(child)
    return nodes


def _positions(text: str, nodes: list[_Node]):
    state = 0
    for start in range(len(text) - 1, -1, -1):
        state = _advance(nodes, state, text[start])
        if nodes[state].count:
            yield start, state


def hit_page(title: str, content: str, terms: list[str], offset: int) -> tuple[int, list[dict]]:
    if not terms:
        return 0, []
    nodes = _matcher(terms)
    hits = []
    total = 0
    for name, text in (("title", title), ("content", content)):
        previous_total = total
        total += sum(nodes[state].count for _, state in _positions(text, nodes))
        if previous_total >= offset + PAGE_SIZE or total <= offset:
            continue
        # 已知总数，倒序读取时可跳过本页以外的整组命中，保持内存只与词表和页长有关。
        ordinal = total
        page = []
        for start, state in _positions(text, nodes):
            if ordinal <= offset:
                break
            begin = ordinal - nodes[state].count
            if begin < offset + PAGE_SIZE:
                while state:
                    node = nodes[state]
                    if node.term is not None:
                        ordinal -= 1
                        if offset <= ordinal < offset + PAGE_SIZE:
                            end = start + len(node.term)
                            page.append({"field": name, "term": node.term, "start": start, "end": end,
                                         "context": text[max(0, start - 16):end + 16]})
                    state = node.fallback
            ordinal = begin
        hits.extend(reversed(page))
    return total, hits
