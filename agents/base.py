"""
Agent 基类 — 所有智能体的公共抽象
"""
from __future__ import annotations
import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    from llm.llm_client import LLMClient, LLMMessage


class AgentRole(str, Enum):
    DIRECTOR = "director"
    SCREENWRITER = "screenwriter"
    ART_DIRECTOR = "art_director"
    CINEMATOGRAPHER = "cinematographer"
    STORYBOARDER = "storyboarder"
    ASSET_BUILDER = "asset_builder"
    VIDEO_RENDERER = "video_renderer"
    SOUND_DESIGNER = "sound_designer"
    POST_EDITOR = "post_editor"
    QA_REVIEWER = "qa_reviewer"
    DIALOGUE_EXPERT = "dialogue_expert"


class Message:
    """Agent间传递的消息"""

    def __init__(
        self,
        sender: str,
        recipient: str,
        content: str,
        msg_type: str = "task",
        metadata: Optional[dict] = None,
    ):
        self.id = str(uuid.uuid4())[:8]
        self.sender = sender
        self.recipient = recipient
        self.content = content
        self.msg_type = msg_type
        self.metadata = metadata or {}
        self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "id": self.id, "sender": self.sender, "recipient": self.recipient,
            "content": self.content, "msg_type": self.msg_type,
            "metadata": self.metadata, "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        msg = cls(data["sender"], data["recipient"], data["content"],
                  data.get("msg_type", "task"), data.get("metadata"))
        msg.id = data.get("id", msg.id)
        msg.timestamp = data.get("timestamp", msg.timestamp)
        return msg

    def __repr__(self):
        return f"Message({self.sender}→{self.recipient}, type={self.msg_type})"


class AgentBase:
    """所有Agent的基类

    子类只需：
    1. 实现 get_full_system_prompt() 返回角色描述
    2. 可选覆盖 build_prompt() 定制上下文注入

    基类自动处理：process() → build_prompt() → call_llm → save → artifact → send
    """

    _default_output_config: dict = {
        "output_file": "output.md",
        "artifact_key": "output",
        "recipient": "",
        "msg_type": "task",
        "metadata": {},
    }

    def __init__(
        self,
        role: AgentRole,
        name: str,
        system_prompt: str,
        project_dir: str | Path,
        llm_client: Optional["LLMClient"] = None,
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        shared_memory=None,
    ):
        self.role = role
        self.name = name
        self.system_prompt = system_prompt
        self.project_dir = Path(project_dir)
        self.output_dir = self.project_dir / "output" / role.value
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.inbox: list[Message] = []
        self.outbox: list[Message] = []
        self.memory: list[dict] = []
        self.status = "idle"
        self.output_config = getattr(self.__class__, 'output_config',
                                     self.__class__._default_output_config).copy()
        self.llm_client = llm_client
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.shared_memory = shared_memory

    def set_output_dir(self, new_project_dir: str | Path):
        self.project_dir = Path(new_project_dir)
        self.output_dir = self.project_dir / "output" / self.role.value
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def call_llm(self, user_message: str, system_override: str = "") -> str:
        """调用LLM生成回复"""
        if self.llm_client is None:
            return f"[模拟模式] {self.name} 收到任务：{user_message[:100]}..."

        system_prompt = system_override or self.get_full_system_prompt()
        from llm.llm_client import LLMMessage
        messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=user_message),
        ]

        try:
            response = self.llm_client.chat_with_retry(
                messages=messages, model=self.model,
                temperature=self.temperature, max_tokens=self.max_tokens,
                max_retries=5, base_delay=3.0,
            )
            self.memory.append({
                "type": "llm_call",
                "user_message": user_message[:500],
                "assistant_response": response.content[:500],
                "model": response.model, "usage": response.usage,
            })
            return response.content
        except Exception as e:
            import logging
            logging.getLogger("agent").error("Agent %s LLM调用失败: %s", self.name, e)
            raise RuntimeError(f"[LLM调用失败] {self.name}: {str(e)[:200]}") from e

    def call_llm_with_split(self, user_message: str, system_override: str = "",
                            ending_markers: list[str] | None = None,
                            expected_sections: list[str] | None = None,
                            max_splits: int = 3) -> str:
        """自动续写：当输出可能被截断时，追加'继续'请求"""
        result = self.call_llm(user_message, system_override)
        for _ in range(max_splits):
            # 简单启发式：如果明显未完成，继续请求
            if len(result) < 200:
                break
            if ending_markers and any(result.strip().endswith(m) for m in ending_markers):
                break
            if expected_sections and all(s in result for s in expected_sections):
                break
            # 追加继续请求
            continuation = self.call_llm(
                "请从上次中断的地方继续生成，不要重复已有内容。", system_override)
            if not continuation.strip():
                break
            result += "\n\n" + continuation
        return result

    def get_full_system_prompt(self) -> str:
        """子类覆盖：返回完整的系统提示词"""
        return self.system_prompt

    def check_completeness(self, content: str, stage: str = "") -> tuple[bool, str]:
        """结构完整性检查：验证输出不为空且有基本结构。
        各 Agent 可覆盖此方法添加角色特定的结构要求。"""
        if not content or not content.strip():
            return False, "输出为空"
        if len(content.strip()) < 50:
            return False, f"输出过短（{len(content.strip())} 字符），可能不完整"
        # 检查是否有至少一个标题（Markdown # 开头）
        if "\n" in content and "#" not in content:
            return False, "缺少 Markdown 标题结构，建议使用 # 标题组织内容"
        return True, ""

    def build_prompt(self, msg: Message, brief: dict) -> str:
        """子类可覆盖：根据消息和项目档案构建用户提示"""
        return msg.content

    def save_output(self, content: str, filename: str = ""):
        """保存Agent产出到文件；覆盖前自动快照旧版本（供 /diff、/restore 使用）"""
        fname = filename or self.output_config.get("output_file", "output.md")
        path = self.output_dir / fname
        if path.exists():
            try:
                from utils.versioning import save_version
                save_version(path)
            except Exception:
                pass  # 快照失败不影响正常保存
        path.write_text(content, encoding="utf-8")
        return path

    # ── 消息收发契约（engine / CLI 调用入口）──

    def receive(self, msg: Message):
        """接收一条消息放入收件箱"""
        self.inbox.append(msg)

    def send(self, recipient: str, content: str, msg_type: str = "task",
             metadata: Optional[dict] = None) -> Message:
        """构造一条消息、放入发件箱并返回（供 CLI /send 与 bus.publish 使用）"""
        msg = Message(self.name, recipient, content, msg_type, metadata)
        self.outbox.append(msg)
        return msg

    def get_status(self) -> dict:
        """返回 Agent 当前状态摘要"""
        return {
            "name": self.name,
            "role": self.role.value,
            "status": self.status,
            "model": self.model,
            "llm": self.llm_client is not None,
            "inbox": len(self.inbox),
            "outbox": len(self.outbox),
        }

    def load_prompt(self) -> str:
        """从 templates/default/prompts 加载本角色的 system prompt；找不到则返回当前。"""
        try:
            from utils.templates import TEMPLATES_DIR
            role_to_file = {
                "director": "01_director.md", "screenwriter": "02_screenwriter.md",
                "art_director": "03_art_director.md", "cinematographer": "04_cinematographer.md",
                "storyboarder": "05_storyboarder.md", "asset_builder": "06_asset_builder.md",
                "sound_designer": "07_sound_designer.md", "qa_reviewer": "08_qa_reviewer.md",
                "video_renderer": "09_video_renderer.md", "post_editor": "10_post_editor.md",
                "dialogue_expert": "11_dialogue_expert.md",
            }
            fname = role_to_file.get(self.role.value)
            if fname:
                path = TEMPLATES_DIR / "default" / "prompts" / fname
                if path.exists():
                    return path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return self.system_prompt

    def process(self) -> list[Message]:
        """处理收件箱中所有消息，返回回复列表（无参版本，供 engine / CLI 调用）。

        流程：取项目档案 → 逐条 _process_one → 清空收件箱 → 返回回复。
        """
        brief = self.shared_memory.get_project_brief() if self.shared_memory else {}
        replies: list[Message] = []
        for msg in list(self.inbox):
            self.status = "working"
            reply = self._process_one(msg, brief)
            replies.append(reply)
            self.status = "done"
        self.inbox.clear()
        return replies

    def _process_one(self, msg: Message, brief: dict) -> Message:
        """单条消息处理：构建提示 → 调用LLM → 保存(md+json) → 记录 → 构造回复"""
        prompt = self.build_prompt(msg, brief)
        output = self.call_llm(prompt)
        md_path = self.save_output(output)
        # 若输出含结构化 JSON，额外落盘 <同名>.json（供媒体执行层消费）
        self._save_json_artifact(output)

        # 记录到共享记忆（agent=中文名, stage=英文 role value）
        if self.shared_memory:
            self.shared_memory.add_artifact(
                self.name, self.role.value, output,
                filepath=str(md_path) if md_path else "",
            )

        # 构造回复消息（合并 output_config 与上游 metadata）
        meta = dict(self.output_config.get("metadata", {}) or {})
        if getattr(msg, "metadata", None):
            meta.update({"stage": msg.metadata.get("stage", meta.get("stage", ""))})
        recipient = self.output_config.get("recipient", "")
        reply = Message(
            sender=self.name, recipient=recipient,
            content=output, msg_type=self.output_config.get("msg_type", "result"),
            metadata=meta,
        )
        self.outbox.append(reply)
        return reply

    def _save_json_artifact(self, output: str) -> Optional[str]:
        """若输出含 JSON 块，解析并保存为 <output_file_stem>.json，返回路径；否则 None"""
        try:
            from utils.json_block import extract_json_block
            data = extract_json_block(output)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        md_name = self.output_config.get("output_file", "output.md")
        json_path = self.output_dir / f"{Path(md_name).stem}.json"
        try:
            json_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return str(json_path)
        except Exception:
            return None

    def reset(self):
        """重置Agent状态"""
        self.inbox = []
        self.outbox = []
        self.memory = []
        self.status = "idle"
