"""
工作流引擎 — 定义和执行AIGC短片的标准化制作流程
对应课程中的"标准化生成视频的流程"
"""
from __future__ import annotations
from enum import Enum
from typing import Optional, Callable
from pathlib import Path
import json
import logging
import re
from datetime import datetime

from agents.base import AgentBase, AgentRole, Message
from agents.message_bus import MessageBus
from shared.memory import SharedMemory


class WorkflowStage(str, Enum):
    """工作流阶段 — 适配全能多模态视频生成的新流程"""
    INIT = "init"                       # 项目初始化
    SCRIPT = "script"                   # 剧本创作
    DIALOGUE_REVIEW = "dialogue_review" # 台词诊断+配音标注
    ART_DIRECTION = "art_direction"     # 美术定调（视觉+音色）
    SHOT_DESIGN = "shot_design"         # 镜头设计（摄影+分镜）
    ASSET_BUILD = "asset_build"         # 资产构建（角色图+音色+场景图）
    VIDEO_RENDER = "video_render"       # 视频直出（多模态输入→音画一体）
    POST_EDIT = "post_edit"             # 后期合成（拼接+BGM+混音）
    QA_REVIEW = "qa_review"             # 质控评审
    FINAL = "final"                     # 最终交付


# 阶段流转定义
STAGE_FLOW = {
    WorkflowStage.INIT: [WorkflowStage.SCRIPT],
    WorkflowStage.SCRIPT: [WorkflowStage.DIALOGUE_REVIEW],
    WorkflowStage.DIALOGUE_REVIEW: [WorkflowStage.ART_DIRECTION],
    WorkflowStage.ART_DIRECTION: [WorkflowStage.SHOT_DESIGN],
    WorkflowStage.SHOT_DESIGN: [WorkflowStage.ASSET_BUILD],
    WorkflowStage.ASSET_BUILD: [WorkflowStage.VIDEO_RENDER],
    WorkflowStage.VIDEO_RENDER: [WorkflowStage.POST_EDIT],
    WorkflowStage.POST_EDIT: [WorkflowStage.QA_REVIEW],
    WorkflowStage.QA_REVIEW: [WorkflowStage.FINAL],
    WorkflowStage.FINAL: [],
}

# 每个阶段负责的Agent
STAGE_AGENTS = {
    WorkflowStage.INIT: [AgentRole.DIRECTOR],
    WorkflowStage.SCRIPT: [AgentRole.SCREENWRITER, AgentRole.DIRECTOR],
    WorkflowStage.DIALOGUE_REVIEW: [AgentRole.DIALOGUE_EXPERT, AgentRole.DIRECTOR],
    WorkflowStage.ART_DIRECTION: [AgentRole.ART_DIRECTOR, AgentRole.DIRECTOR],
    WorkflowStage.SHOT_DESIGN: [AgentRole.CINEMATOGRAPHER, AgentRole.STORYBOARDER, AgentRole.DIRECTOR],
    WorkflowStage.ASSET_BUILD: [AgentRole.ASSET_BUILDER, AgentRole.DIRECTOR],
    WorkflowStage.VIDEO_RENDER: [AgentRole.VIDEO_RENDERER, AgentRole.DIRECTOR],
    WorkflowStage.POST_EDIT: [AgentRole.SOUND_DESIGNER, AgentRole.POST_EDITOR, AgentRole.DIRECTOR],
    WorkflowStage.QA_REVIEW: [AgentRole.QA_REVIEWER, AgentRole.DIRECTOR],
    WorkflowStage.FINAL: [AgentRole.DIRECTOR],
}

# Agent消息路由表
AGENT_ROUTES = {
    AgentRole.SCREENWRITER: "台词专家",      # 编剧产出直接传给台词专家
    AgentRole.DIALOGUE_EXPERT: "美术指导",
    AgentRole.CINEMATOGRAPHER: "分镜师",
    AgentRole.STORYBOARDER: "资产构建师",
    AgentRole.ASSET_BUILDER: "视频渲染师",
    AgentRole.VIDEO_RENDERER: "后期剪辑师",
    AgentRole.SOUND_DESIGNER: "后期剪辑师",
    AgentRole.POST_EDITOR: "质控评审",
}

# 阶段入口播种：每个阶段的"主 Agent"在收件箱为空时注入的默认任务消息。
# 上游内容由各 Agent 的 build_prompt 从 SharedMemory.artifacts 拉取，不依赖跨阶段总线消息。
STAGE_SEEDS: dict[WorkflowStage, tuple[AgentRole, str]] = {
    WorkflowStage.SCRIPT: (AgentRole.SCREENWRITER,
        "请根据项目档案创作完整剧本：故事梗概、角色设定、世界观、分场大纲、完整剧本正文。"),
    WorkflowStage.DIALOGUE_REVIEW: (AgentRole.DIALOGUE_EXPERT,
        "请对剧本进行台词诊断，标注每句的情绪强度、配音语速/音量与表演建议。"),
    WorkflowStage.ART_DIRECTION: (AgentRole.ART_DIRECTOR,
        "请产出美术定调方案：调色板(含HEX)、视觉风格关键词、场景氛围、角色视觉。"),
    WorkflowStage.SHOT_DESIGN: (AgentRole.CINEMATOGRAPHER,
        "请为各场戏设计镜头方案：景别、焦段、运镜、光线方向。"),
    WorkflowStage.ASSET_BUILD: (AgentRole.ASSET_BUILDER,
        "请构建角色与场景视觉资产，并输出资产清单 JSON（每条含英文图片 prompt，角色末尾 white background）。"),
    WorkflowStage.VIDEO_RENDER: (AgentRole.VIDEO_RENDERER,
        "请为每个镜头生成视频提示词，并输出视频渲染计划 JSON（id/prompt/num_frames/frame_rate 等）。"),
    WorkflowStage.POST_EDIT: (AgentRole.POST_EDITOR,
        "请产出后期剪辑方案：时间线、转场、调色、音画同步。"),
    WorkflowStage.QA_REVIEW: (AgentRole.QA_REVIEWER,
        "请对全片产出做最终质控评审：结构完整性、逻辑一致性、格式规范、质量标准。"),
}

# 视频渲染师的额外上游数据源（通过SharedMemory读取，不改变消息路由）
# 注意：视频渲染师的上游数据（摄影指导/分镜师）通过 SharedMemory.artifacts 拉取，
# 不经 MessageBus 路由。见 VideoRendererAgent.build_prompt 中的 get_artifacts_by_agent 调用。

# Gate Review revise 时的默认回退目标
ROLLBACK_TARGETS = {
    WorkflowStage.SCRIPT: WorkflowStage.SCRIPT,
    WorkflowStage.DIALOGUE_REVIEW: WorkflowStage.SCRIPT,
    WorkflowStage.ART_DIRECTION: WorkflowStage.SCRIPT,
    WorkflowStage.SHOT_DESIGN: WorkflowStage.ART_DIRECTION,
    WorkflowStage.ASSET_BUILD: WorkflowStage.SHOT_DESIGN,
    WorkflowStage.VIDEO_RENDER: WorkflowStage.ASSET_BUILD,
    WorkflowStage.POST_EDIT: WorkflowStage.VIDEO_RENDER,
    WorkflowStage.QA_REVIEW: WorkflowStage.POST_EDIT,
}

# 软性Gate前置条件 — 每个阶段建议满足的必要产出（仅警告，不阻断）
# 格式：{stage: [(agent_name, required_keywords_in_artifact), ...]}
GATE_PREREQUISITES = {
    WorkflowStage.DIALOGUE_REVIEW: [
        ("编剧", ["场景", "S1", "第一幕", "剧本"]),  # 编剧必须有场景描述
    ],
    WorkflowStage.ART_DIRECTION: [
        ("编剧", ["场景", "S1", "第一幕"]),  # 编剧必须有场景
    ],
    WorkflowStage.SHOT_DESIGN: [
        ("美术指导", ["#", "色", "主"]),  # 美术指导必须有色彩定义
    ],
    WorkflowStage.ASSET_BUILD: [
        ("分镜师", ["景别", "L0", "镜头", "机位"]),  # 分镜师必须有镜头列表
    ],
    WorkflowStage.VIDEO_RENDER: [
        ("资产构建师", ["角色", "场景", "Prompt"]),  # 资产构建师必须有资产
    ],
    WorkflowStage.POST_EDIT: [
        ("视频渲染师", ["段落", "提示词", "风格"]),  # 视频渲染师必须有提示词
    ],
    WorkflowStage.QA_REVIEW: [
        ("后期剪辑师", ["时间线", "转场", "段落"]),  # 后期剪辑师必须有方案
    ],
}


class WorkflowEngine:
    """工作流引擎，管理AIGC短片的制作流程"""

    def __init__(self, project_dir: str | Path):
        self.project_dir = Path(project_dir)
        self.project_name = ""
        self.bus = MessageBus()
        self.memory = SharedMemory(project_dir)
        self.agents: dict[AgentRole, AgentBase] = {}
        self.current_stage: WorkflowStage = WorkflowStage.INIT
        self.stage_history: list[dict] = []
        self.hooks: dict[str, list[Callable]] = {}
        # 媒体生成（Agnes 图片/视频/拼接）
        self.media_cfg: dict = {}
        self.media_clients: dict = {"image": None, "video": None, "text": None}
        self.media_model: str = ""

    def switch_project(self, project_dir: str | Path, project_name: str = ""):
        """切换到另一个项目"""
        self.project_dir = Path(project_dir)
        self.project_name = project_name
        self.memory.switch_project(project_dir)
        for agent in self.agents.values():
            agent.set_output_dir(project_dir)
        self.current_stage = WorkflowStage.INIT
        self.stage_history = []
        self.bus = MessageBus()

    def register_agent(self, agent: AgentBase):
        """注册Agent到工作流"""
        self.agents[agent.role] = agent

    def get_agent(self, role: AgentRole) -> Optional[AgentBase]:
        """获取指定角色的Agent"""
        return self.agents.get(role)

    # ── 媒体生成接入（Agnes 图片/视频/拼接）──

    def register_media(self, cfg: dict, image_client=None, video_client=None,
                       text_client=None, model: str = ""):
        """注册媒体配置与客户端，供 ASSET_BUILD/VIDEO_RENDER/POST_EDIT 阶段调用。"""
        self.media_cfg = cfg or {}
        self.media_clients = {"image": image_client, "video": video_client, "text": text_client}
        self.media_model = model

    def run_media(self, stage: WorkflowStage) -> dict:
        """对指定阶段执行媒体生成。返回结果 dict。"""
        from media import executor
        log = logging.getLogger("media")
        cfg = self.media_cfg or {}
        if not cfg.get("enabled"):
            return {"stage": stage.value, "skipped": True, "reason": "media disabled"}

        img = self.media_clients.get("image")
        vid = self.media_clients.get("video")
        txt = self.media_clients.get("text")
        try:
            if stage == WorkflowStage.ASSET_BUILD:
                if not img:
                    return {"stage": stage.value, "skipped": True, "reason": "no image client"}
                log.info("ASSET_BUILD → 生成资产参考图")
                res = executor.generate_assets(self.memory, self.project_dir, cfg, img, txt, self.media_model)
            elif stage == WorkflowStage.VIDEO_RENDER:
                if not vid:
                    return {"stage": stage.value, "skipped": True, "reason": "no video client"}
                log.info("VIDEO_RENDER → 生成镜头视频")
                res = executor.generate_videos(self.memory, self.project_dir, cfg, vid, txt, self.media_model)
            elif stage == WorkflowStage.POST_EDIT:
                log.info("POST_EDIT → 拼接成片")
                res = executor.merge_final(self.project_dir, cfg)
            else:
                return {"stage": stage.value, "skipped": True, "reason": "no media action for stage"}
        except Exception as e:
            log.error("媒体生成失败 (%s): %s", stage.value, e, exc_info=True)
            res = {"error": str(e)[:300]}
        res["stage"] = stage.value
        return res

    def media_summary(self) -> dict:
        """汇总媒体产出（供状态展示）。总数取自计划文件，完成数取自实际产出文件。"""
        import json as _json
        out = self.project_dir / "output"
        summary = {"enabled": bool((self.media_cfg or {}).get("enabled"))}
        # 视频：计划总数(video_render_plan) vs 已完成(media_videos)
        plan = out / "video_renderer" / "video_render_plan.json"
        if plan.exists():
            try:
                summary["videos_total"] = len(_json.loads(plan.read_text(encoding="utf-8")).get("shots", []))
            except Exception:
                pass
        mv = out / "video_renderer" / "media_videos.json"
        if mv.exists():
            try:
                shots = _json.loads(mv.read_text(encoding="utf-8")).get("shots", [])
                summary["videos_completed"] = sum(1 for s in shots if s.get("status") == "completed")
            except Exception:
                pass
        # 图片：计划总数(asset_manifest) vs 已完成(media_assets)
        manifest = out / "asset_builder" / "asset_manifest.json"
        if manifest.exists():
            try:
                m = _json.loads(manifest.read_text(encoding="utf-8"))
                summary["images_total"] = sum(len(m.get(c, []) or []) for c in ("characters", "scenes", "props"))
            except Exception:
                pass
        ma = out / "asset_builder" / "media_assets.json"
        if ma.exists():
            try:
                idx = _json.loads(ma.read_text(encoding="utf-8"))
                summary["images"] = sum(len(idx.get(c, []) or []) for c in ("characters", "scenes", "props"))
            except Exception:
                pass
        final = out / "post_editor" / "final.mp4"
        if final.exists():
            summary["final"] = str(final)
        return summary

    def add_hook(self, event: str, callback: Callable):
        """添加事件钩子（before_stage / after_stage / on_decision）"""
        if event not in self.hooks:
            self.hooks[event] = []
        self.hooks[event].append(callback)

    def _run_hooks(self, event: str, **kwargs):
        """执行事件钩子"""
        for callback in self.hooks.get(event, []):
            callback(**kwargs)

    def advance_stage(self, target_stage: Optional[WorkflowStage] = None) -> WorkflowStage:
        """
        推进到下一个阶段
        如果不指定target_stage，则按STAGE_FLOW自动推进
        """
        self._run_hooks("before_stage", stage=self.current_stage)

        if target_stage:
            self.current_stage = target_stage
        else:
            next_stages = STAGE_FLOW.get(self.current_stage, [])
            if next_stages:
                self.current_stage = next_stages[0]
            else:
                self.current_stage = WorkflowStage.FINAL

        # 记录阶段历史
        self.stage_history.append({
            "stage": self.current_stage.value,
            "agents": [r.value for r in STAGE_AGENTS.get(self.current_stage, [])],
        })

        # 更新共享记忆
        self.memory.update_work_context("current_stage", self.current_stage.value)

        self._run_hooks("after_stage", stage=self.current_stage)
        return self.current_stage

    def rollback_stage(self, target_stage: Optional[WorkflowStage] = None, reason: str = "") -> WorkflowStage:
        """
        回退到前一阶段（Gate Review revise 时使用）

        Args:
            target_stage: 回退目标阶段（None则按ROLLBACK_TARGETS自动推断）
            reason: 回退原因（记入历史）

        Returns:
            回退后的当前阶段
        """
        log = logging.getLogger("engine")

        if target_stage is None:
            target_stage = ROLLBACK_TARGETS.get(self.current_stage, WorkflowStage.SCRIPT)

        prev_stage = self.current_stage
        self.current_stage = target_stage

        # 重置目标阶段的Agent状态
        target_agents = STAGE_AGENTS.get(target_stage, [])
        for role in target_agents:
            agent = self.agents.get(role)
            if agent:
                agent.reset()
                log.info("回退重置 Agent: %s", agent.name)

        # 记录回退历史
        self.stage_history.append({
            "action": "rollback",
            "from": prev_stage.value,
            "to": target_stage.value,
            "reason": reason,
        })

        self.memory.update_work_context("current_stage", self.current_stage.value)
        log.info("阶段回退: %s → %s (原因: %s)", prev_stage.value, target_stage.value, reason)

        self._run_hooks("on_rollback", from_stage=prev_stage, to_stage=target_stage, reason=reason)
        return self.current_stage

    def send_task(self, sender_role: AgentRole, recipient_role: AgentRole, content: str, metadata: Optional[dict] = None):
        """在Agent间发送任务"""
        sender = self.agents.get(sender_role)
        recipient = self.agents.get(recipient_role)
        if sender and recipient:
            msg = sender.send(recipient.name, content, "task", metadata)
            self.bus.publish(msg)

    def run_stage(self, stage: Optional[WorkflowStage] = None) -> dict:
        """
        执行指定阶段的工作流
        返回阶段执行结果
        """
        return self.run_stage_async(stage)

    def run_stage_async(
        self,
        stage: Optional[WorkflowStage] = None,
        on_agent_start: Optional[Callable] = None,
        on_agent_done: Optional[Callable] = None,
    ) -> dict:
        """
        执行指定阶段的工作流（支持回调通知）
        可从线程中调用，通过回调通知进度
        支持重复执行：自动 reset 阶段内 agents
        """
        if stage:
            self.current_stage = stage

        stage_agents = STAGE_AGENTS.get(self.current_stage, [])

        # 硬性前置条件检查
        prerequisites = GATE_PREREQUISITES.get(self.current_stage, [])
        if prerequisites:
            missing = []
            for agent_name, required_keywords in prerequisites:
                artifacts = self.memory.get_artifacts_by_agent(agent_name)
                if not artifacts:
                    missing.append(f"{agent_name}无产出")
                    continue
                latest = artifacts[-1].get("content", "")
                found = any(kw in latest for kw in required_keywords)
                if not found:
                    missing.append(f"{agent_name}产出缺少必要内容（需要：{', '.join(required_keywords)}）")
            if missing:
                log = logging.getLogger("engine")
                log.warning("阶段 %s 前置条件警告（继续执行）: %s", self.current_stage.value, missing)

        # 重跑支持：reset 阶段内的 agents + 清空当前阶段的待处理消息
        for role in stage_agents:
            agent = self.agents.get(role)
            if agent:
                agent.reset()
                agent.inbox = []
                agent.outbox = []
                agent.status = "idle"
                # 清空该 Agent 的待处理队列，但不销毁整个 MessageBus
                self.bus.consume(agent.name)
        result = {
            "stage": self.current_stage.value,
            "agents": [r.value for r in stage_agents],
            "messages": [],
            "decisions": [],
            "errors": [],
        }

        # 阶段入口播种：主 Agent 收件箱为空时注入默认任务（上游由 build_prompt 从 artifacts 拉取）
        seed = STAGE_SEEDS.get(self.current_stage)
        if seed:
            seed_role, seed_task = seed
            seed_agent = self.agents.get(seed_role)
            if seed_agent and not self.bus.peek(seed_agent.name):
                self.bus.publish(Message(
                    "system", seed_agent.name, seed_task, "task",
                    {"stage": self.current_stage.value},
                ))

        # 获取当前阶段需要工作的Agent
        for role in stage_agents:
            agent = self.agents.get(role)
            if agent:
                # 回调：Agent开始
                if on_agent_start:
                    on_agent_start(role.value)

                # 消费消息
                messages = self.bus.consume(agent.name)
                for msg in messages:
                    agent.receive(msg)

                # 处理消息（带错误恢复）
                try:
                    agent.status = "working"
                    output_messages = agent.process()
                    for msg in output_messages:
                        # 按 AGENT_ROUTES 覆盖task类型消息的接收者
                        if msg.msg_type == "task" and role in AGENT_ROUTES:
                            msg.recipient = AGENT_ROUTES[role]
                        self.bus.publish(msg)
                        result["messages"].append(msg.to_dict())
                    agent.status = "done"
                except Exception as e:
                    agent.status = "error"
                    result["errors"].append({
                        "agent": role.value,
                        "error": str(e)[:500],
                    })
                    logging.getLogger("engine").error(
                        "Agent %s 失败: %s", agent.name, e, exc_info=True
                    )

                # 回调：Agent完成
                if on_agent_done:
                    on_agent_done(role.value)

        # 媒体生成（资产图/视频/拼接）—— 阶段文本产出完成且无错误时触发
        if (self.current_stage in (WorkflowStage.ASSET_BUILD, WorkflowStage.VIDEO_RENDER, WorkflowStage.POST_EDIT)
                and not result.get("errors")
                and (self.media_cfg or {}).get("enabled")):
            result["media"] = self.run_media(self.current_stage)

        return result

    def gate_review(self, stage: WorkflowStage) -> dict:
        """
        阶段关卡审查（对应课程的Gate Review）
        由总导演+质控评审执行
        """
        log = logging.getLogger("engine")

        director = self.agents.get(AgentRole.DIRECTOR)
        reviewer = self.agents.get(AgentRole.QA_REVIEWER)

        review_result = {
            "stage": stage.value,
            "passed": False,
            "issues": [],
            "reviewer_output": "",
            "director_output": "",
            "decision": "skipped",  # approve / revise / skipped
        }

        if not reviewer or not director:
            log.warning("Gate Review [%s] 跳过：未注册质控评审或总导演", stage.value)
            review_result["decision"] = "skipped"
            review_result["passed"] = True  # 无人审查时放行，避免阻塞
            return review_result

        reviewer_issues = []

        # 质控评审执行检查（直投receive，不走bus避免双重投递）
        if reviewer:
            review_msg = Message(
                "system", reviewer.name,
                f"请对 {stage.value} 阶段产出执行结构检查和6项多模态一致性检查",
                "task", {"stage": stage.value}
            )
            reviewer.receive(review_msg)
            reviewer.status = "working"
            try:
                outputs = reviewer.process()
                all_reviewer_output = []
                for msg in outputs:
                    if msg.metadata.get("review_result"):
                        reviewer_issues = msg.metadata["review_result"].get("issues", [])
                    all_reviewer_output.append(msg.content)
                review_result["reviewer_output"] = "\n\n".join(all_reviewer_output)
                reviewer.status = "done"
            except Exception as e:
                reviewer.status = "error"
                log.error("Gate Review 质控评审失败: %s", e, exc_info=True)

        review_result["issues"] = reviewer_issues

        # 总导演做最终决策（直投receive，不走bus避免双重投递）
        if director:
            decision_msg = Message(
                "system", director.name,
                f"请对 {stage.value} 阶段产出做Gate Review决策",
                "task", {
                    "stage": stage.value,
                    "issues": reviewer_issues,
                    "review_result": review_result,
                }
            )
            director.receive(decision_msg)
            director.status = "working"
            try:
                outputs = director.process()
                all_director_output = []
                decision_found = False
                for msg in outputs:
                    self.bus.publish(msg)
                    all_director_output.append(msg.content)
                    # 只取第一个带决策的消息
                    decision = msg.metadata.get("decision", "")
                    if decision in ("approve", "revise"):
                        review_result["decision"] = decision
                        review_result["passed"] = (decision == "approve")
                        decision_found = True
                        break
                if not decision_found:
                    # 没有任何消息带回决策，按issues兜底
                    review_result["decision"] = "approve" if not reviewer_issues else "revise"
                    review_result["passed"] = not bool(reviewer_issues)
                review_result["director_output"] = "\n\n".join(all_director_output)
                director.status = "done"
            except Exception as e:
                director.status = "error"
                log.error("Gate Review 总导演失败: %s", e, exc_info=True)
                review_result["decision"] = "approve"
                review_result["passed"] = True

        return review_result

    def get_status(self) -> dict:
        """获取工作流当前状态"""
        return {
            "current_stage": self.current_stage.value,
            "agents": {role.value: agent.get_status() for role, agent in self.agents.items()},
            "bus_stats": self.bus.get_stats(),
            "stage_history": self.stage_history,
            "media": self.media_summary(),
        }

    def save_status(self):
        """保存工作流状态"""
        status_file = self.project_dir / "output" / "workflow_status.json"
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text(
            json.dumps(self.get_status(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 如果当前阶段是FINAL，且有系列记忆，自动追忆本集摘要
        if self.current_stage == WorkflowStage.FINAL and self.memory.series_dir:
            self._auto_append_series_memory()

    def _auto_append_series_memory(self):
        """FINAL阶段后自动生成本集摘要并追加到系列记忆"""
        brief = self.memory.get_project_brief()
        # 从编剧产出中提取摘要信息
        script_artifacts = self.memory.get_artifacts_by_agent("编剧")
        script_preview = ""
        if script_artifacts:
            script_preview = script_artifacts[-1]["content"][:2000]

        episode_summary = {
            "episode": brief.get("current_episode", brief.get("project_name", "")),
            "title": brief.get("project_name", ""),
            "summary": brief.get("logline", "")[:200],
            "core_emotions": brief.get("core_emotions", []),
            "emotional_arc": " → ".join(brief.get("core_emotions", [])),
            "character_development": "",
            "used_jokes": [],
            "script_preview": script_preview[:500],
            "timestamp": datetime.now().isoformat(),
        }

        self.memory.append_series_memory(episode_summary)
        log = logging.getLogger("engine")
        log.info("已自动追忆本集摘要到系列记忆")

    def export_shot_plan(self) -> dict:
        """
        导出结构化拍摄计划（JSON），供外部工具或API模式使用

        Returns:
            {
                "project": {...项目信息...},
                "references": {...已锁定的参考图...},
                "shots": [{id, image_prompt, video_prompt, ...}, ...],
                "timeline": {...剪辑方案...},
            }
        """
        brief = self.memory.get_project_brief()
        refs = self.memory.get_references()

        # 解析资产构建产出 → 提取每个镜头的提示词
        image_artifacts = self.memory.get_artifacts_by_agent("资产构建师") or self.memory.get_artifacts_by_stage("asset_build")
        video_artifacts = self.memory.get_artifacts_by_agent("视频渲染师") or self.memory.get_artifacts_by_stage("video_render")
        sound_artifacts = self.memory.get_artifacts_by_agent("声音设计师") or self.memory.get_artifacts_by_stage("sound_design")

        shots = []
        # 从图片产出中提取镜头（按 L01/L02 编号）
        if image_artifacts:
            content = image_artifacts[-1]["content"]
            current_shot = None
            for line in content.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # 检测镜头编号 L01 或 SB-01
                match = re.match(r"(?:L|SB-|shot_|镜头)[\s-]*(\d+)", line, re.IGNORECASE)
                if match:
                    if current_shot:
                        shots.append(current_shot)
                    num = match.group(1).zfill(2)
                    current_shot = {"id": f"L{num}", "image_prompt": "", "video_prompt": ""}
                    # 提取同行的提示词
                    prompt_part = line[match.end():].strip().lstrip(":：").strip()
                    if prompt_part:
                        current_shot["image_prompt"] = prompt_part
                elif current_shot and line:
                    current_shot["image_prompt"] += (" " + line) if current_shot["image_prompt"] else line
            if current_shot:
                shots.append(current_shot)

        # 从视频产出中补充视频提示词
        if video_artifacts:
            content = video_artifacts[-1]["content"]
            vid_shots = {}
            current_id = None
            for line in content.split("\n"):
                line = line.strip()
                match = re.match(r"(?:L|SB-|shot_|镜头|Clip)[\s-]*(\d+)", line, re.IGNORECASE)
                if match:
                    num = match.group(1).zfill(2)
                    current_id = f"L{num}"
                    vid_shots[current_id] = line[match.end():].strip().lstrip(":：").strip()
                elif current_id and line:
                    vid_shots[current_id] += " " + line
            # 合并到 shots
            shot_map = {s["id"]: s for s in shots}
            for shot_id, vprompt in vid_shots.items():
                if shot_id in shot_map:
                    shot_map[shot_id]["video_prompt"] = vprompt
                else:
                    shots.append({"id": shot_id, "image_prompt": "", "video_prompt": vprompt})

        # 如果没有任何镜头，返回空但结构完整
        if not shots:
            shots = [{"id": "L01", "image_prompt": "(尚未生成)", "video_prompt": "(尚未生成)"}]

        # 附加参考图信息到每个镜头
        char_refs = refs.get("characters", {})
        for shot in shots:
            shot["character_refs"] = list(char_refs.keys())
            shot["scene_refs"] = list(refs.get("scenes", {}).keys())

        plan = {
            "project": {
                "name": brief.get("project_name", ""),
                "type": brief.get("work_type", ""),
                "logline": brief.get("logline", ""),
            },
            "references": refs,
            "shots": shots,
        }

        # 保存到文件
        plan_file = self.project_dir / "output" / "shot_plan.json"
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

        return plan
