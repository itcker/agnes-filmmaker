"""重跑指定项目的流水线（支持全量重跑和增量重跑）"""
from __future__ import annotations
import sys
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from main import create_team, load_config
from workflow.engine import WorkflowStage, STAGE_AGENTS, AGENT_ROUTES
from agents.base import Message
from utils.templates import apply_template


# 阶段顺序（用于增量重跑时确定要清除哪些阶段）
STAGE_ORDER = [
    WorkflowStage.SCRIPT,
    WorkflowStage.DIALOGUE_REVIEW,
    WorkflowStage.ART_DIRECTION,
    WorkflowStage.SHOT_DESIGN,
    WorkflowStage.ASSET_BUILD,
    WorkflowStage.VIDEO_RENDER,
    WorkflowStage.POST_EDIT,
    WorkflowStage.QA_REVIEW,
    WorkflowStage.FINAL,
]


def _reseed_bus_from_upstream(engine, target_stage: WorkflowStage):
    """从 SharedMemory 恢复上游 Agent 的产出到 MessageBus，使目标阶段 Agent 能正常消费"""
    # 构建反向路由：目标 Agent 名 → 上游 Agent 角色
    reverse_routes = {}
    for sender_role, recipient_name in AGENT_ROUTES.items():
        reverse_routes[recipient_name] = sender_role

    target_agents = STAGE_AGENTS.get(target_stage, [])
    for role in target_agents:
        agent = engine.agents.get(role)
        if not agent:
            continue
        # 查找是否有上游 Agent 路由到此 Agent
        sender_role = reverse_routes.get(agent.name)
        if sender_role:
            sender_agent = engine.agents.get(sender_role)
            if sender_agent:
                artifacts = engine.memory.get_artifacts_by_agent(sender_agent.name)
                if artifacts:
                    content = artifacts[-1]["content"]
                    msg = Message(sender_agent.name, agent.name, content, "task")
                    engine.bus.publish(msg)
        # 无上游路由的 Agent（如 Director）会通过 kickoff_prompt 自行生成内容


def _run_pipeline_from_stage(engine, start_stage: WorkflowStage):
    """从指定阶段运行到 FINAL"""
    start_idx = STAGE_ORDER.index(start_stage)
    for stage in STAGE_ORDER[start_idx:]:
        print(f"\n>> 阶段: {stage.value}")
        result = engine.run_stage(stage)

        if result.get("errors"):
            print(f"  [!] 有 {len(result['errors'])} 个 Agent 失败: "
                  f"{[e['agent'] for e in result['errors']]}")

        engine.advance_stage()

    engine.save_status()
    return engine.get_status()


def rerun(project_name: str, template_key: str = "", from_stage: str = ""):
    project_dir = PROJECT_ROOT / "projects" / project_name
    if not project_dir.exists():
        print(f"项目不存在: {project_dir}")
        return

    if from_stage:
        # ── 增量重跑模式 ──
        target_stage = WorkflowStage(from_stage)
        print(f"=== 增量重跑项目: {project_name} (从 {from_stage} 开始) ===\n")

        # 1. 只清空目标阶段及之后的 output 目录
        output_dir = project_dir / "output"
        target_idx = STAGE_ORDER.index(target_stage)
        stages_to_clear = STAGE_ORDER[target_idx:]
        for stage in stages_to_clear:
            for role in STAGE_AGENTS.get(stage, []):
                agent_dir = output_dir / role.value
                if agent_dir.exists():
                    shutil.rmtree(agent_dir)
                    agent_dir.mkdir(parents=True, exist_ok=True)
                    print(f"  清除: {role.value}/")

        # 2. 从 SharedMemory 中删除目标阶段及之后的 artifacts
        engine_tmp = create_team(project_dir, load_config())
        engine_tmp.memory.artifacts = [
            a for a in engine_tmp.memory.artifacts
            if a.get("stage") not in [s.value for s in stages_to_clear]
        ]
        engine_tmp.memory._save()
        print("  已清理目标阶段的 artifacts 记录")

        # 3. 创建引擎、恢复上游消息
        print("\n初始化团队...")
        config = load_config()
        engine = create_team(project_dir, config)

        if template_key:
            print(f"应用模板: {template_key}")
            result = apply_template(engine, template_key)
            print(f"  已加载 {result.get('applied_prompts', 0)} 个 Agent Prompt")

        # 4. 恢复 bus 消息
        engine.current_stage = target_stage
        _reseed_bus_from_upstream(engine, target_stage)
        print("已恢复上游消息到 MessageBus")

        # 5. 从目标阶段运行到结束
        print(f"\n从阶段 {from_stage} 开始运行...\n")
        status = _run_pipeline_from_stage(engine, target_stage)

    else:
        # ── 全量重跑模式 ──
        print(f"=== 重跑项目: {project_name} ===\n")

        output_dir = project_dir / "output"
        if output_dir.exists():
            print("清空旧输出...")
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        shared_dir = project_dir / "shared"
        for f in shared_dir.glob("*.json"):
            if f.name != "project_brief.json":
                f.unlink()
                print(f"  清除: {f.name}")

        print("\n初始化团队...")
        config = load_config()
        engine = create_team(project_dir, config)

        if template_key:
            print(f"应用模板: {template_key}")
            result = apply_template(engine, template_key)
            print(f"  已加载 {result.get('applied_prompts', 0)} 个 Agent Prompt")
            print(f"  模板名: {result.get('template_name', '')}")

        engine.current_stage = WorkflowStage.SCRIPT
        print("\n开始运行完整流水线...\n")
        status = _run_pipeline_from_stage(engine, WorkflowStage.SCRIPT)

    # 导出拍摄计划
    print("\n导出拍摄计划...")
    plan = engine.export_shot_plan()
    print(f"  镜头数: {len(plan.get('shots', []))}")

    print(f"\n=== 完成！项目: {project_name} ===")
    print(f"输出目录: {output_dir}")
    return status


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="重跑AIGC项目")
    parser.add_argument("project", help="项目名")
    parser.add_argument("--template", default="", help="模板key (default)")
    parser.add_argument("--from-stage", default="",
        help="从指定阶段开始增量重跑 (script/dialogue_review/art_direction/shot_design/asset_build/video_render/post_edit/qa_review/final)")
    args = parser.parse_args()

    rerun(args.project, args.template, args.from_stage)
