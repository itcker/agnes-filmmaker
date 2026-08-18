"""产出预览 — 基础解析，提取标题和结构。纯工具代码，无领域知识。"""
from __future__ import annotations
import re
from pathlib import Path


def preview_agent(project_dir: Path, agent_key: str) -> dict | None:
    """预览单个 agent 的产出"""
    agent_dir = project_dir / "output" / agent_key
    if not agent_dir.exists():
        return None
    files = []
    for f in sorted(agent_dir.glob("*.md")):
        content = f.read_text(encoding="utf-8")
        files.append({
            "name": f.name,
            "size": len(content),
            "headings": _extract_headings(content),
            "preview": content[:2000],
        })
    return {"agent": agent_key, "files": files}


def preview_all(project_dir: Path) -> list[dict]:
    """预览所有 agent 产出"""
    output_dir = project_dir / "output"
    if not output_dir.exists():
        return []
    results = []
    for d in sorted(output_dir.iterdir()):
        if d.is_dir():
            preview = preview_agent(project_dir, d.name)
            if preview:
                results.append(preview)
    return results


def _extract_headings(content: str) -> list[str]:
    """提取 Markdown 标题"""
    headings = []
    for line in content.split("\n"):
        m = re.match(r"^(#{1,4})\s+(.+)", line)
        if m:
            headings.append(f"{'  ' * (len(m.group(1)) - 1)}{m.group(2)}")
    return headings[:30]  # 最多 30 个标题
