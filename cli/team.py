"""
cli.team — 创建智能体团队、初始化项目、运行完整流水线（agnes-filmmaker）
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from agents.implementations import (
    DirectorAgent,
    ScreenwriterAgent,
    ArtDirectorAgent,
    CinematographerAgent,
    StoryboarderAgent,
    AssetBuilderAgent,
    VideoRendererAgent,
    SoundDesignerAgent,
    PostEditorAgent,
    QAReviewerAgent,
    DialogueExpertAgent,
)
from llm.llm_client import LLMClient
from utils.logger import get_logger
from workflow.engine import WorkflowEngine, WorkflowStage, STAGE_AGENTS

from cli.config import load_config, _resolve_provider

# yaml 用于写入 config.yaml（可选依赖）
try:
    import yaml
except ImportError:
    yaml = None

log = get_logger("team")


def create_team(
    project_dir: str | Path,
    config: dict | None = None,
    llm_clients: dict[str, LLMClient] | None = None,
) -> WorkflowEngine:
    """
    创建完整的 agnes-filmmaker 智能体团队

    Args:
        project_dir: 项目目录
        config: 项目配置（None则从config.yaml读取）
        llm_clients: LLM客户端映射（None则根据config自动创建）

    Returns:
        WorkflowEngine 实例
    """
    from cli.config import create_llm_clients as _create_llm_clients

    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)

    if config is None:
        config = load_config()

    # 初始化LLM客户端
    if llm_clients is None:
        llm_clients = _create_llm_clients(config)

    # 初始化工作流引擎
    engine = WorkflowEngine(project_dir)
    shared_memory = engine.memory

    # 从配置中获取每个Agent的LLM设置
    agent_configs = config.get("agents", {})

    def get_llm_kwargs(agent_name: str) -> dict:
        """获取Agent的LLM调用参数"""
        cfg = agent_configs.get(agent_name, {})
        model = cfg.get("model", "")
        temperature = cfg.get("temperature", 0.7)
        max_tokens = cfg.get("max_tokens", 2048)
        explicit_provider = cfg.get("provider", "")

        provider = _resolve_provider(config, model, explicit_provider)
        client = llm_clients.get(provider)

        return {
            "llm_client": client,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    # 创建所有Agent（传入LLM配置）
    agents = [
        DirectorAgent(project_dir, shared_memory=shared_memory, **get_llm_kwargs("director")),
        ScreenwriterAgent(project_dir, shared_memory=shared_memory, **get_llm_kwargs("screenwriter")),
        ArtDirectorAgent(project_dir, shared_memory=shared_memory, **get_llm_kwargs("art_director")),
        CinematographerAgent(project_dir, shared_memory=shared_memory, **get_llm_kwargs("cinematographer")),
        StoryboarderAgent(project_dir, shared_memory=shared_memory, **get_llm_kwargs("storyboarder")),
        AssetBuilderAgent(project_dir, shared_memory=shared_memory, **get_llm_kwargs("asset_builder")),
        VideoRendererAgent(project_dir, shared_memory=shared_memory, **get_llm_kwargs("video_renderer")),
        SoundDesignerAgent(project_dir, shared_memory=shared_memory, **get_llm_kwargs("sound_designer")),
        PostEditorAgent(project_dir, shared_memory=shared_memory, **get_llm_kwargs("post_editor")),
        QAReviewerAgent(project_dir, shared_memory=shared_memory, **get_llm_kwargs("qa_reviewer")),
        DialogueExpertAgent(project_dir, shared_memory=shared_memory, **get_llm_kwargs("dialogue_expert")),
    ]

    for agent in agents:
        engine.register_agent(agent)
        # 加载对应的System Prompt
        loaded_prompt = agent.load_prompt()
        if loaded_prompt != agent.system_prompt:
            agent.system_prompt = loaded_prompt

    # 媒体生成客户端（Agnes 图片/视频）+ 文本客户端（JSON 归一化兜底）
    media_cfg = config.get("media", {}) or {}
    if media_cfg.get("enabled"):
        try:
            from media.agnes_image import AgnesImageClient
            from media.agnes_video import AgnesVideoClient
            agnes_cfg = (config.get("llm_providers", {}) or {}).get("agnes", {}) or {}
            api_key = agnes_cfg.get("api_key") or os.environ.get("AGNES_KEY", "")
            base_url = agnes_cfg.get("base_url") or os.environ.get(
                "AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")
            img_client = AgnesImageClient(api_key=api_key, base_url=base_url)
            vid_client = AgnesVideoClient(api_key=api_key, base_url=base_url)
            txt_client = llm_clients.get("agnes")
            txt_model = ""
            for ac in (config.get("agents", {}) or {}).values():
                if isinstance(ac, dict) and ac.get("model"):
                    txt_model = ac["model"]
                    break
            engine.register_media(media_cfg, img_client, vid_client, txt_client, txt_model)
            log.info("媒体生成已启用（图片/视频/拼接）")
        except Exception as e:
            log.warning("媒体客户端初始化失败，媒体生成将禁用: %s", e)

    return engine


def init_project(engine: WorkflowEngine, project_config: dict):
    """
    初始化项目（对应课程的"项目档案"建立阶段）
    """
    engine.memory.init_project_brief(project_config)
    engine.advance_stage(WorkflowStage.SCRIPT)

    log = get_logger("project")
    log.info("项目初始化完成: %s", project_config.get('project_name', ''))
    log.info("当前阶段: %s", engine.current_stage.value)
    log.info("核心情绪: %s", project_config.get('core_emotions', []))
    log.info("不变量: %s", project_config.get('invariants', []))


def run_full_pipeline(engine: WorkflowEngine, max_revisions: int = 3):
    """
    运行完整制作流程
    """
    log = get_logger("pipeline")
    stages = [
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

    log.info("agnes-filmmaker — 制作流程启动")

    stage_idx = 0
    while stage_idx < len(stages):
        stage = stages[stage_idx]
        log.info("阶段: %s  负责Agent: %s", stage.value, [r.value for r in STAGE_AGENTS.get(stage, [])])

        # 执行阶段
        result = engine.run_stage(stage)
        if result.get("errors"):
            log.warning("阶段 %s 有 %d 个Agent失败: %s",
                stage.value, len(result["errors"]),
                [e["agent"] for e in result["errors"]])

        # 关卡审查（非首尾阶段）
        if stage not in [WorkflowStage.INIT, WorkflowStage.FINAL]:
            log.info("执行Gate Review...")
            review = engine.gate_review(stage)

            if review.get("passed"):
                log.info("审查结果: 通过")
            else:
                revision_count = 0
                target = stage
                while not review.get("passed") and revision_count < max_revisions:
                    revision_count += 1
                    log.info("审查未通过，第 %d 次修改...", revision_count)
                    engine.rollback_stage(target, f"Gate Review 第{revision_count}次修改")
                    engine.run_stage(target)
                    review = engine.gate_review(target)

                if review.get("passed"):
                    log.info("修改后审查通过")
                else:
                    log.warning("经过 %d 次修改仍未通过，继续下一阶段", max_revisions)

        engine.advance_stage()
        stage_idx += 1

    log.info("制作流程完成！")

    # 保存最终状态
    engine.save_status()

    return engine.get_status()


def _parse_brief_with_llm(engine: WorkflowEngine, user_input: str) -> dict | None:
    """用LLM将用户的大白话解析为结构化项目配置"""
    # 找一个有LLM的Agent，同时获取其model
    llm_client = None
    llm_model = ""
    for agent in engine.agents.values():
        if agent.llm_client:
            llm_client = agent.llm_client
            llm_model = getattr(agent, "model", "")
            break
    if not llm_client:
        return None

    system_prompt = (
        "你是一个项目配置解析器。用户会用大白话描述一个AI视频/短片的想法，"
        "你需要把它解析为JSON格式的项目配置。\n\n"
        "只输出JSON，不要任何解释。JSON格式如下：\n"
        '{\n'
        '  "project_name": "简短的项目名称（2-6个字）",\n'
        '  "work_type": "AI短片" 或 "AI漫剧" 或 "MV" 或 "广告",\n'
        '  "episode_plan": "单集X分钟xN集",\n'
        '  "logline": "一句话故事概括",\n'
        '  "core_emotions": ["情绪1", "情绪2"],\n'
        '  "core_tags": ["标签1", "标签2", "标签3"],\n'
        '  "invariants": ["规则1", "规则2"],\n'
        '  "worldview": "世界观描述"\n'
        '}\n\n'
        "规则：\n"
        "- project_name 要简洁有记忆点\n"
        "- core_emotions 2-4个，从故事中提取\n"
        "- core_tags 2-5个，关键词\n"
        "- invariants 是用户明确要求的规则，没有就写空数组\n"
        "- 如果用户没提时长，默认单集1分钟x1集\n"
        "- 如果用户没提类型，默认AI短片"
    )

    try:
        from llm.llm_client import LLMMessage
        messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=user_input),
        ]
        response = llm_client.chat(messages, model=llm_model, temperature=0.3, max_tokens=1024)
        text = response.content.strip()

        # 提取JSON（兼容markdown code block包裹的情况）
        if "```" in text:
            start = text.find("```")
            start = text.find("\n", start) + 1
            end = text.find("```", start)
            text = text[start:end].strip()

        return json.loads(text)
    except Exception as e:
        log = get_logger("project")
        log.warning("LLM解析失败: %s", e)
        return None
