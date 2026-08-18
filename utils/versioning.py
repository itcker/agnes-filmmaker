"""版本管理 — 自动备份、差异对比、版本恢复。纯工具代码，无领域知识。"""
from __future__ import annotations
import difflib
import json
from datetime import datetime
from pathlib import Path


def list_versions(filepath: Path) -> list[dict]:
    """列出文件的所有历史版本"""
    versions_dir = filepath.parent / ".versions"
    if not versions_dir.exists():
        return []
    base = filepath.name
    versions = []
    for vf in sorted(versions_dir.glob(f"{base}.*.bak"), reverse=True):
        tag = vf.name[len(base) + 1:-len(".bak")]
        versions.append({"tag": tag, "path": str(vf), "size": vf.stat().st_size})
    return versions


def list_all_versions(output_dir: Path) -> list[dict]:
    """列出所有 agent 产出的版本"""
    results = []
    for agent_dir in sorted(output_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        for f in sorted(agent_dir.glob("*.md")):
            versions = list_versions(f)
            if versions:
                results.append({"agent": agent_dir.name, "file": f.name, "versions": versions})
    return results


def diff_versions(filepath: Path, v1: str = "", v2: str = "") -> dict:
    """对比两个版本或当前版本与上一版本"""
    versions = list_versions(filepath)
    if not versions:
        return {"error": "暂无历史版本"}
    current = filepath.read_text(encoding="utf-8") if filepath.exists() else ""

    if v1 and v2:
        # 两个指定版本对比
        v1_file = filepath.parent / ".versions" / f"{filepath.name}.{v1}.bak"
        v2_file = filepath.parent / ".versions" / f"{filepath.name}.{v2}.bak"
        if not v1_file.exists() or not v2_file.exists():
            return {"error": "版本文件不存在"}
        text1 = v1_file.read_text(encoding="utf-8")
        text2 = v2_file.read_text(encoding="utf-8")
    elif v1:
        # 指定版本 vs 当前
        v1_file = filepath.parent / ".versions" / f"{filepath.name}.{v1}.bak"
        if not v1_file.exists():
            return {"error": f"版本 {v1} 不存在"}
        text1 = v1_file.read_text(encoding="utf-8")
        text2 = current
    else:
        # 上一版本 vs 当前
        prev_file = filepath.parent / ".versions" / f"{filepath.name}.{versions[0]['tag']}.bak"
        text1 = prev_file.read_text(encoding="utf-8")
        text2 = current

    diff = list(difflib.unified_diff(
        text1.splitlines(keepends=True),
        text2.splitlines(keepends=True),
        fromfile=f"v{versions[0]['tag'] if not v1 else v1}",
        tofile="current" if not v2 else f"v{v2}",
    ))
    return {"diff": "".join(diff), "added": sum(1 for l in diff if l.startswith("+")),
            "removed": sum(1 for l in diff if l.startswith("-"))}


def restore_version(filepath: Path, version_tag: str) -> dict:
    """恢复到指定版本"""
    backup = filepath.parent / ".versions" / f"{filepath.name}.{version_tag}.bak"
    if not backup.exists():
        return {"error": f"版本 {version_tag} 不存在"}
    # 先备份当前版本
    save_version(filepath)
    # 恢复
    content = backup.read_text(encoding="utf-8")
    filepath.write_text(content, encoding="utf-8")
    return {"status": "ok", "restored": version_tag}


def save_version(filepath: Path) -> str | None:
    """保存当前文件的版本快照"""
    if not filepath.exists():
        return None
    versions_dir = filepath.parent / ".versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = versions_dir / f"{filepath.name}.{tag}.bak"
    n = 1
    while backup.exists():  # 同秒多次覆盖时追加序号，避免快照互相覆盖
        backup = versions_dir / f"{filepath.name}.{tag}_{n}.bak"
        n += 1
    backup.write_bytes(filepath.read_bytes())
    return backup.name[len(filepath.name) + 1:-len(".bak")]
