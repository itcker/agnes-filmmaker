"""
cli.projects — 项目注册表（projects.json）CRUD 及旧项目迁移
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from utils.logger import get_logger

from cli.config import PROJECT_ROOT

# 项目注册表文件路径
PROJECTS_FILE = PROJECT_ROOT / "projects.json"


def load_projects() -> dict:
    """读取项目注册表"""
    if PROJECTS_FILE.exists():
        return json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    return {}


def save_projects(projects: dict):
    """写入项目注册表（跨进程原子写：临时文件 + os.replace + 文件锁）"""
    import os
    data = json.dumps(projects, ensure_ascii=False, indent=2)
    tmp = PROJECTS_FILE.with_suffix(".tmp")
    try:
        import portalocker
        with portalocker.Lock(str(PROJECTS_FILE.with_suffix(".lock")), "w", timeout=10):
            tmp.write_text(data, encoding="utf-8")
            os.replace(str(tmp), str(PROJECTS_FILE))
    except ImportError:
        # portalocker 未装时退化为原子 replace（无跨进程互斥）
        tmp.write_text(data, encoding="utf-8")
        os.replace(str(tmp), str(PROJECTS_FILE))


def sanitize_project_name(name: str) -> str:
    """清洗项目名：去文件系统非法字符 + 路径穿越（../），限长 30。"""
    import re
    cleaned = re.sub(r'[\\/:*?"<>|\n\r\t]', "", str(name)).replace("..", "").strip().replace(" ", "_")
    return cleaned[:30] or "未命名"


def get_project_dir(name: str) -> Path:
    """获取项目目录路径（清洗路径穿越字符，防 ../../../ 攻击）。"""
    cleaned = sanitize_project_name(name)
    return PROJECT_ROOT / "projects" / cleaned


def read_current_stage(project_dir) -> str:
    """从 workflow_status.json 读 current_stage（删 engine 后查询端点的来源）。无则空串。"""
    status_file = Path(project_dir) / "output" / "workflow_status.json"
    if not status_file.exists():
        return ""
    try:
        return json.loads(status_file.read_text(encoding="utf-8")).get("current_stage", "")
    except Exception:
        return ""


def migrate_old_project() -> str | None:
    """
    迁移旧格式项目到 projects/ 目录
    检测：根目录下 shared/project_brief.json 存在 -> 旧项目
    返回迁移的项目名，无旧项目返回 None
    """
    old_brief = PROJECT_ROOT / "shared" / "project_brief.json"
    if not old_brief.exists():
        return None

    try:
        brief = json.loads(old_brief.read_text(encoding="utf-8"))
        name = brief.get("project_name", "") or "未命名项目"
    except Exception:
        name = "未命名项目"

    project_dir = get_project_dir(name)
    if project_dir.exists():
        # 已经迁移过了
        return None

    project_dir.mkdir(parents=True, exist_ok=True)

    # 迁移 shared/ 下的 JSON 数据文件（不动 .py 源码）
    new_shared = project_dir / "shared"
    new_shared.mkdir(exist_ok=True)
    old_shared = PROJECT_ROOT / "shared"
    for f in old_shared.glob("*.json"):
        target = new_shared / f.name
        if not target.exists():
            shutil.move(str(f), str(target))

    # 迁移 output/ 整个目录
    old_output = PROJECT_ROOT / "output"
    new_output = project_dir / "output"
    if old_output.is_dir() and old_output.exists():
        for item in old_output.iterdir():
            target = new_output / item.name
            if not target.exists():
                shutil.move(str(item), str(target))

    # 注册到 projects.json
    projects = load_projects()
    if name not in projects:
        projects[name] = {"created_at": datetime.now().isoformat()}
        save_projects(projects)

    log = get_logger("project")
    log.info("已迁移旧项目 '%s' 到 projects/%s/", name, name)
    return name
