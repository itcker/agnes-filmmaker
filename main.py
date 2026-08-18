"""
主程序 — agnes-filmmaker 入口（Agnes AI 全链路短片制作）
支持三种使用方式：
1. 交互式命令行：python main.py
2. Python API：from main import create_team, init_project
3. 一键全流程：python main.py --pipeline

功能实现已拆分到 cli 包下的各个模块：
  - cli.config     配置加载、LLM provider 解析
  - cli.projects   项目注册表 CRUD
  - cli.team       团队创建、项目初始化、流水线
  - cli.interactive 交互式 REPL 及所有 /command
  - cli.main       主入口 main()
"""
from __future__ import annotations

# ── 向后兼容：保持 `from main import ...` 不变 ──
from cli.config import (
    DEFAULT_PROJECT_CONFIG,
    PROJECT_ROOT,
    load_config,
    _resolve_provider,
    create_llm_clients,
)
from cli.projects import (
    load_projects,
    save_projects,
    get_project_dir,
    migrate_old_project,
)
from cli.team import (
    create_team,
    init_project,
    run_full_pipeline,
    _parse_brief_with_llm,
)
from cli.interactive import (
    interactive_mode,
    new_project_interactive,
    _new_project_manual,
    _apply_new_project,
    switch_model_interactive,
    _print_preview,
)
from cli.main import main


if __name__ == "__main__":
    main()
