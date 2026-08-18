"""
LLM 客户端 — Agnes AI 专用。

文本模型 agnes-2.5-flash，走 OpenAI 兼容协议（POST /v1/chat/completions），
Base URL https://apihub.agnes-ai.com/v1。支持普通对话、function calling、流式。
图片/视频生成见 media/ 包。
"""
from __future__ import annotations
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LLMMessage:
    """统一消息格式（支持 function calling）"""
    role: str  # system / user / assistant / tool
    content: str = ""
    tool_calls: list = field(default_factory=list)  # assistant 消息携带的 tool_calls
    tool_call_id: str = ""  # role=tool 消息关联的 tool_call id


@dataclass
class LLMResponse:
    """统一响应格式"""
    content: str
    model: str = ""
    usage: dict = field(default_factory=dict)
    tool_calls: list = field(default_factory=list)  # function calling 结果
    raw: Optional[dict] = None


class LLMClient(ABC):
    """LLM 客户端抽象基类"""

    @abstractmethod
    def chat(
        self,
        messages: list[LLMMessage],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """发送对话请求"""
        ...

    def simple_chat(
        self,
        system_prompt: str,
        user_message: str,
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> str:
        """便捷方法：system + user 两条消息，返回文本"""
        messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=user_message),
        ]
        response = self.chat(messages, model, temperature, max_tokens)
        return response.content

    def chat_with_retry(
        self,
        messages: list[LLMMessage],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> LLMResponse:
        """带指数退避重试的对话请求"""
        last_error = None
        for attempt in range(max_retries):
            try:
                return self.chat(messages, model, temperature, max_tokens)
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    time.sleep(delay)
        raise last_error

    def chat_with_tools(
        self,
        messages: list[LLMMessage],
        model: str = "",
        tools: Optional[list] = None,
        tool_choice: str = "auto",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """带 function calling 的对话（子类按需实现）。默认不支持。"""
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 tool use")


class AgnesClient(LLMClient):
    """Agnes AI 客户端（OpenAI 兼容接口）。

    文本模型 agnes-2.5-flash，Base URL https://apihub.agnes-ai.com/v1。
    支持 chat / chat_with_tools / chat_with_tools_stream。
    """

    DEFAULT_MODEL = "agnes-2.5-flash"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.api_key = (
            api_key
            or os.environ.get("AGNES_KEY", "")
            or os.environ.get("AGNES_API_KEY", "")
        )
        self.base_url = base_url or os.environ.get(
            "AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1"
        )
        self._client = None

    def _get_client(self):
        """懒加载 OpenAI 兼容客户端"""
        if self._client is None:
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            except ImportError:
                raise ImportError(
                    "需要安装 openai 库：pip install openai\n"
                    "或使用 pip install -r requirements.txt 安装所有依赖"
                )
        return self._client

    def chat(
        self,
        messages: list[LLMMessage],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        client = self._get_client()
        model = model or self.DEFAULT_MODEL
        msg_dicts = [self._msg_to_openai(m) for m in messages]

        response = client.chat.completions.create(
            model=model,
            messages=msg_dicts,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        return LLMResponse(
            content=response.choices[0].message.content or "",
            model=response.model,
            usage={
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                "total_tokens": getattr(response.usage, "total_tokens", 0),
            },
            raw=response.model_dump(),
        )

    def chat_with_tools(
        self,
        messages: list[LLMMessage],
        model: str = "",
        tools: Optional[list] = None,
        tool_choice: str = "auto",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """OpenAI 兼容的 function calling。"""
        client = self._get_client()
        model = model or self.DEFAULT_MODEL
        msg_dicts = [self._msg_to_openai(m) for m in messages]
        kwargs = {"model": model, "messages": msg_dicts,
                  "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        response = client.chat.completions.create(**kwargs)
        choice_msg = response.choices[0].message
        tool_calls = []
        if choice_msg.tool_calls:
            for tc in choice_msg.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "",
                })
        return LLMResponse(
            content=choice_msg.content or "",
            model=response.model,
            tool_calls=tool_calls,
            usage={
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                "total_tokens": getattr(response.usage, "total_tokens", 0),
            },
            raw=response.model_dump(),
        )

    def chat_with_tools_stream(
        self,
        messages: list[LLMMessage],
        model: str = "",
        tools: Optional[list] = None,
        tool_choice: str = "auto",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        """流式 function calling — 以 dict 事件产出。

        事件：{"type":"token","content":"..."} |
              {"type":"done","content":"...","tool_calls":[...],"usage":{...},"model":"..."}
        """
        client = self._get_client()
        model = model or self.DEFAULT_MODEL
        msg_dicts = [self._msg_to_openai(m) for m in messages]
        kwargs = {"model": model, "messages": msg_dicts,
                  "temperature": temperature, "max_tokens": max_tokens,
                  "stream": True}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        response = client.chat.completions.create(**kwargs)
        accumulated_tool_calls = {}
        content_parts = []
        final_usage = {}
        final_model = model

        for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue
            if delta.content:
                content_parts.append(delta.content)
                yield {"type": "token", "content": delta.content}
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in accumulated_tool_calls:
                        accumulated_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        accumulated_tool_calls[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            accumulated_tool_calls[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            accumulated_tool_calls[idx]["arguments"] += tc.function.arguments
            if hasattr(chunk, "usage") and chunk.usage:
                final_usage = {
                    "prompt_tokens": getattr(chunk.usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(chunk.usage, "completion_tokens", 0),
                    "total_tokens": getattr(chunk.usage, "total_tokens", 0),
                }
            if hasattr(chunk, "model") and chunk.model:
                final_model = chunk.model

        tool_calls_list = [tc for _, tc in sorted(accumulated_tool_calls.items())]
        yield {
            "type": "done",
            "content": "".join(content_parts),
            "tool_calls": tool_calls_list,
            "usage": final_usage,
            "model": final_model,
        }

    @staticmethod
    def _msg_to_openai(m: LLMMessage) -> dict:
        """LLMMessage → OpenAI 消息 dict（处理 assistant.tool_calls 与 tool 角色）"""
        if m.role == "assistant" and m.tool_calls:
            return {
                "role": "assistant",
                "content": m.content or None,
                "tool_calls": [{"id": tc["id"], "type": "function",
                                "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                               for tc in m.tool_calls],
            }
        if m.role == "tool":
            return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
        return {"role": m.role, "content": m.content}


def create_llm_client(provider: str = "agnes", **kwargs) -> LLMClient:
    """工厂方法：创建 Agnes LLM 客户端。

    provider 参数仅为向后兼容保留，始终返回 AgnesClient。
    """
    return AgnesClient(**{k: v for k, v in kwargs.items() if k in ("api_key", "base_url")})
