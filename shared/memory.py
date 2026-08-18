"""
共享记忆 — 跨Agent的项目档案与上下文管理
对应课程中的"项目档案.md"和"上下文管理工作流"
"""
from __future__ import annotations
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class SharedMemory:
    """
    共享记忆系统，实现课程中的三阶段上下文管理：
    1. 项目档案（外置记忆）→ 作为System Prompt
    2. 工作上下文 → 当前任务相关材料
    3. 生成环节 → 基于前两阶段产出新内容
    """

    def __init__(self, project_dir: str | Path):
        self.project_dir = Path(project_dir)
        self.memory_dir = self.project_dir / "shared"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        # 系列目录（如果项目在 系列名/EPXX/ 下，则系列名为父目录）
        self.series_dir: Optional[Path] = None
        self.series_memory: list[dict] = []
        self.series_brief: dict = {}

        # 项目档案（长期记忆）
        self.project_brief: dict = {}
        # 工作上下文（会话记忆）
        self.work_context: dict = {}
        # 产出记录
        self.artifacts: list[dict] = []
        # 参考图锁定（解决角色/场景一致性）
        self.references: dict = {"characters": {}, "scenes": {}, "style": {}}
        # 角色音色资产（voice_id + 参考音频 + 音色参数）
        self.voice_profiles: dict = {}

        self._lock = threading.Lock()
        self._load()

    def _detect_series_dir(self) -> Optional[Path]:
        """检测项目是否在系列目录下（projects/系列名/EPXX/）"""
        # 如果项目目录的父目录是 projects/ 下的一级目录，且该目录下有 shared/series_brief.json
        parent = self.project_dir.parent
        if parent != Path(".") and parent.parent.name == "projects" and parent.name != "projects":
            series_brief = parent / "shared" / "series_brief.json"
            if series_brief.exists():
                return parent
        return None

    def get_series_memory_text(self) -> str:
        """获取系列记忆的可读文本（注入给编剧用）"""
        if not self.series_memory:
            return ""
        lines = []
        for ep in self.series_memory:
            lines.append(f"### {ep.get('episode', '?')}：{ep.get('title', '')}")
            lines.append(f"- 核心内容：{ep.get('summary', '')}")
            lines.append(f"- 角色成长：{ep.get('character_development', '')}")
            lines.append(f"- 情绪弧线：{ep.get('emotional_arc', '')}")
            used = ep.get("used_jokes", [])
            if used:
                lines.append(f"- 已用笑点/桥段：{', '.join(used)}")
            lines.append("")
        return "\n".join(lines)

    def append_series_memory(self, episode_summary: dict):
        """在系列记忆中追加一集的摘要"""
        if not self.series_dir:
            return
        memory_file = self.series_dir / "shared" / "series_memory.json"
        if memory_file.exists():
            memory = json.loads(memory_file.read_text(encoding="utf-8"))
        else:
            memory = {"episodes": []}

        # 避免重复
        ep_id = episode_summary.get("episode", "")
        memory["episodes"] = [e for e in memory["episodes"] if e.get("episode") != ep_id]
        memory["episodes"].append(episode_summary)

        with self._lock:
            memory_file.write_text(
                json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        self.series_memory = memory["episodes"]

    def _load(self):
        """从磁盘加载已有记忆"""
        # 加载系列级记忆
        self.series_dir = self._detect_series_dir()
        if self.series_dir:
            series_brief_file = self.series_dir / "shared" / "series_brief.json"
            if series_brief_file.exists():
                self.series_brief = json.loads(series_brief_file.read_text(encoding="utf-8"))
            series_memory_file = self.series_dir / "shared" / "series_memory.json"
            if series_memory_file.exists():
                sm = json.loads(series_memory_file.read_text(encoding="utf-8"))
                self.series_memory = sm.get("episodes", [])

        brief_file = self.memory_dir / "project_brief.json"
        if brief_file.exists():
            self.project_brief = json.loads(brief_file.read_text(encoding="utf-8"))

        context_file = self.memory_dir / "work_context.json"
        if context_file.exists():
            self.work_context = json.loads(context_file.read_text(encoding="utf-8"))

        artifacts_file = self.memory_dir / "artifacts.json"
        if artifacts_file.exists():
            self.artifacts = json.loads(artifacts_file.read_text(encoding="utf-8"))

        refs_file = self.memory_dir / "references.json"
        if refs_file.exists():
            self.references = json.loads(refs_file.read_text(encoding="utf-8"))

        voice_file = self.memory_dir / "voice_profiles.json"
        if voice_file.exists():
            self.voice_profiles = json.loads(voice_file.read_text(encoding="utf-8"))

    def _save(self):
        """保存记忆到磁盘（线程安全）"""
        with self._lock:
            (self.memory_dir / "project_brief.json").write_text(
                json.dumps(self.project_brief, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (self.memory_dir / "work_context.json").write_text(
                json.dumps(self.work_context, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (self.memory_dir / "artifacts.json").write_text(
                json.dumps(self.artifacts, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (self.memory_dir / "references.json").write_text(
                json.dumps(self.references, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (self.memory_dir / "voice_profiles.json").write_text(
                json.dumps(self.voice_profiles, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def update_project_brief(self, key: str, value: Any):
        """更新项目档案（对应课程的Project Brief）"""
        self.project_brief[key] = value
        self._save()

    def get_project_brief(self) -> dict:
        """获取完整项目档案"""
        with self._lock:
            return self.project_brief.copy()

    def init_project_brief(self, brief: dict):
        """
        初始化项目档案，包含课程要求的所有字段
        """
        template = {
            "project_name": brief.get("project_name", ""),
            "work_type": brief.get("work_type", "AI漫剧"),
            "episode_plan": brief.get("episode_plan", "单集3分钟×1集"),
            "core_tags": brief.get("core_tags", []),
            "logline": brief.get("logline", ""),
            "core_emotions": brief.get("core_emotions", []),
            "invariants": brief.get("invariants", []),
            "palette": brief.get("palette", {}),
            "style_keywords": brief.get("style_keywords", {}),
            "characters": brief.get("characters", []),
            "worldview": brief.get("worldview", ""),
        }
        self.project_brief = template
        self._save()

    def switch_project(self, new_dir: str | Path):
        """切换到另一个项目的目录"""
        self.project_dir = Path(new_dir)
        self.memory_dir = self.project_dir / "shared"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.project_brief = {}
        self.work_context = {}
        self.artifacts = []
        self.references = {"characters": {}, "scenes": {}, "style": {}}
        self.voice_profiles = {}
        self._load()

    def reset_brief(self):
        """清空项目档案（新建项目时使用）"""
        self.project_brief = {}
        self.work_context = {}
        self.artifacts = []
        self.references = {"characters": {}, "scenes": {}, "style": {}}
        self._save()

    def update_work_context(self, key: str, value: Any):
        """更新工作上下文（当前任务相关材料）"""
        self.work_context[key] = value
        self._save()

    def add_artifact(self, agent_name: str, stage: str, content: str, filepath: str = ""):
        """记录Agent产出"""
        self.artifacts.append({
            "agent": agent_name,
            "stage": stage,
            "content": content,
            "filepath": filepath,
            "timestamp": datetime.now().isoformat(),
        })
        self._save()

    def get_artifacts_by_stage(self, stage: str) -> list[dict]:
        """按阶段获取产出"""
        with self._lock:
            return [a.copy() for a in self.artifacts if a["stage"] == stage]

    def get_artifacts_by_agent(self, agent_name: str) -> list[dict]:
        """按Agent获取产出（同时匹配中文名 agent 字段与英文 role 的 stage 字段）"""
        with self._lock:
            return [a.copy() for a in self.artifacts
                    if a["agent"] == agent_name or a.get("stage") == agent_name]

    def compress_context(self, max_tokens: int = 500) -> str:
        """
        上下文压缩（对应课程中的"项目档案压缩"方法）
        当对话超过2-3万字时主动触发，压缩为500字内的项目档案
        """
        brief = self.project_brief
        compressed = f"""# 项目档案（压缩版）
项目：{brief.get('project_name', '')}
类型：{brief.get('work_type', '')}
集数：{brief.get('episode_plan', '')}
标签：{', '.join(brief.get('core_tags', []))}
Logline：{brief.get('logline', '')}
核心情绪：{', '.join(brief.get('core_emotions', []))}
不变量：{', '.join(brief.get('invariants', []))}
世界观：{brief.get('worldview', '')}
"""
        # 附加角色摘要
        for char in brief.get("characters", []):
            compressed += f"\n角色：{char.get('name', '')} | {char.get('one_liner', '')}"
        return compressed[:max_tokens]  # 中文约1字=1token

    def clear_work_context(self):
        """清空工作上下文（新阶段开始时）"""
        self.work_context = {}
        self._save()

    # ── 参考图管理（解决角色/场景一致性）──

    def lock_reference(self, category: str, name: str, data: dict):
        """
        锁定一个参考图

        Args:
            category: "characters" / "scenes" / "style"
            name: 参考名称（如角色名、场景名）
            data: {image_path, description, ...}
        """
        if category not in self.references:
            self.references[category] = {}
        data["locked"] = True
        data["locked_at"] = datetime.now().isoformat()
        self.references[category][name] = data
        self._save()

    def get_reference(self, category: str, name: str) -> Optional[dict]:
        """获取单个参考图"""
        with self._lock:
            return self.references.get(category, {}).get(name)

    def get_references(self, category: str = "") -> dict:
        """获取参考图（按类别或全部）"""
        with self._lock:
            if category:
                return self.references.get(category, {}).copy()
            return self.references.copy()

    def unlock_reference(self, category: str, name: str):
        """解锁/删除参考图"""
        if category in self.references:
            self.references[category].pop(name, None)
            self._save()

    # ── 角色音色资产管理 ──

    def set_voice_profile(self, character_name: str, profile: dict):
        """设定角色音色资产"""
        profile["updated_at"] = datetime.now().isoformat()
        self.voice_profiles[character_name] = profile
        self._save()

    def get_voice_profile(self, character_name: str) -> Optional[dict]:
        """获取单个角色音色"""
        with self._lock:
            return self.voice_profiles.get(character_name)

    def get_voice_profiles(self) -> dict:
        """获取全部角色音色"""
        with self._lock:
            return self.voice_profiles.copy()
