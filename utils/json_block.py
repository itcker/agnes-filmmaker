"""
JSON 块解析 — 从 LLM 文本输出中提取结构化 JSON。
兼容：纯 JSON、```json 代码块、Markdown 正文中嵌入的代码块。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional


def extract_json_block(text: str, key: Optional[str] = None) -> Optional[Any]:
    """从文本中提取 JSON。

    Args:
        text: LLM 原始输出
        key: 若指定，则返回解析后 dict 中该键的值（如 "shots"/"characters"）

    Returns:
        解析后的 Python 对象（通常为 dict），失败返回 None。
    """
    if not text or not text.strip():
        return None

    candidates: list[str] = []

    # 1) 任意语言的围栏代码块（```json / ```plaintext / 裸 ``` 等），逐个尝试 JSON 解析
    for m in re.finditer(r"```[a-zA-Z0-9_+-]*\s*\n?(.*?)```", text, re.DOTALL):
        candidates.append(m.group(1).strip())

    # 2) 整体可能是 JSON
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        candidates.append(stripped)

    # 3) 兜底：抓取第一个 {...} 或 [...] 片段
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end != -1 and end > start:
            candidates.append(stripped[start : end + 1])

    for cand in candidates:
        try:
            data = json.loads(cand)
        except Exception:
            # 尝试修复常见问题：尾随逗号
            try:
                cleaned = re.sub(r",\s*([}\]])", r"\1", cand)
                data = json.loads(cleaned)
            except Exception:
                continue
        if key is not None and isinstance(data, dict):
            return data.get(key)
        return data

    return None
