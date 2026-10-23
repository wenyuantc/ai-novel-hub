"""从模型输出中容错地解析 JSON。"""

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


class JSONExtractError(ValueError):
    pass


def extract_json(text: str) -> Any:
    """容错解析：去 ```json 围栏，从首个 [ 或 { 起解析，忽略其后的多余文字。"""
    candidates: list[str] = []
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1))
    # 未闭合的围栏（输出被截断）或无围栏
    candidates.append(re.sub(r"^\s*```(?:json|JSON)?", "", text.strip()))

    decoder = json.JSONDecoder()
    for candidate in candidates:
        starts = sorted(i for i in (candidate.find("["), candidate.find("{")) if i >= 0)
        for start in starts:
            try:
                obj, _ = decoder.raw_decode(candidate[start:])
                return obj
            except json.JSONDecodeError:
                pass
            # 兜底：截到最后一个配对括号
            closing = "]" if candidate[start] == "[" else "}"
            end = candidate.rfind(closing)
            if end > start:
                try:
                    return json.loads(candidate[start : end + 1])
                except json.JSONDecodeError:
                    pass
    raise JSONExtractError("模型输出不是合法的 JSON，无法解析")
