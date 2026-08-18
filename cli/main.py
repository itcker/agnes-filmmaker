"""
cli.main — 程序主入口（argparse + 启动逻辑）
"""
from __future__ import annotations

import argparse

from utils.logger import setup_logging, get_logger
from workflow.engine import WorkflowStage

from cli.config import PROJECT_ROOT, load_config, DEFAULT_PROJECT_CONFIG
from cli.projects import load_projects, get_project_dir, migrate_old_project
from cli.team import create_team, init_project, run_full_pipeline
from cli.interactive import interactive_mode


def main():
    """主入口"""
    setup_logging()
    log = get_logger("main")

    parser = argparse.ArgumentParser(description="agnes-filmmaker")
    parser.add_argument("--pipeline", action="store_true", help="直接运行完整流程")
    parser.add_argument("--project", type=str, default=None, help="项目配置文件路径")
    parser.add_argument("--no-media", action="store_true", help="禁用图片/视频生成，仅跑文本链路")
    args = parser.parse_args()

    project_dir = PROJECT_ROOT

    # 迁移旧项目（如有）
    migrated = migrate_old_project()
    if migrated:
        print(f"已迁移旧项目 '{migrated}' 到 projects/{migrated}/")

    # 加载配置
    config = load_config(args.project)

    # --no-media：关闭媒体生成（仅文本链路）
    if args.no_media:
        config.setdefault("media", {})["enabled"] = False
        log.info("已通过 --no-media 禁用媒体生成")

    log.info("agnes-filmmaker (Agnes AI · 11 Agents)")

    # 创建团队（自动初始化LLM客户端）
    log.info("初始化团队...")
    engine = create_team(project_dir, config)

    # 如果有已注册项目，切换到最新的
    projects = load_projects()
    if projects:
        latest_name = list(projects.keys())[-1]
        latest_dir = get_project_dir(latest_name)
        engine.switch_project(latest_dir, latest_name)
        # 恢复阶段（如果有已有brief说明项目已初始化过）
        brief = engine.memory.get_project_brief()
        if brief:
            engine.current_stage = WorkflowStage.SCRIPT
        log.info("已加载项目: %s", latest_name)
    else:
        # 无已有项目，用 config.yaml 中的项目初始化
        project_config = config.get("project", {}) or DEFAULT_PROJECT_CONFIG
        init_project(engine, project_config)

    # 运行模式选择
    if args.pipeline:
        run_full_pipeline(engine)
    else:
        interactive_mode(engine)
