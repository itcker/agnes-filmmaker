"""
消息总线 — Agent间的通信中心
支持同步和异步消息传递
"""
from __future__ import annotations
import threading
from collections import defaultdict
from typing import Optional, Callable
from .base import Message


class MessageBus:
    """消息总线，负责Agent间的消息路由"""

    def __init__(self):
        self._queues: dict[str, list[Message]] = defaultdict(list)
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._history: list[Message] = []
        self._lock = threading.Lock()

    def publish(self, message: Message):
        """发布消息到目标Agent的队列"""
        with self._lock:
            self._queues[message.recipient].append(message)
            self._history.append(message)
            # 触发订阅回调
            for callback in self._subscribers.get(message.recipient, []):
                callback(message)
            # 广播消息
            if message.recipient == "broadcast":
                for agent_name in self._queues:
                    if agent_name != message.sender:
                        broadcast_msg = Message(
                            message.sender, agent_name, message.content,
                            message.msg_type, message.metadata
                        )
                        self._queues[agent_name].append(broadcast_msg)
                        self._history.append(broadcast_msg)

    def consume(self, agent_name: str) -> list[Message]:
        """消费指定Agent的所有待处理消息"""
        with self._lock:
            messages = self._queues.pop(agent_name, [])
            return messages

    def peek(self, agent_name: str) -> list[Message]:
        """查看指定Agent的待处理消息（不消费）"""
        with self._lock:
            return list(self._queues.get(agent_name, []))

    def subscribe(self, agent_name: str, callback: Callable):
        """订阅指定Agent的消息通知"""
        self._subscribers[agent_name].append(callback)

    def get_history(self, sender: Optional[str] = None, recipient: Optional[str] = None) -> list[Message]:
        """获取消息历史，可按发送者/接收者过滤"""
        result = self._history
        if sender:
            result = [m for m in result if m.sender == sender]
        if recipient:
            result = [m for m in result if m.recipient == recipient]
        return result

    def clear(self):
        """清空所有队列和历史"""
        with self._lock:
            self._queues.clear()
            self._history.clear()

    def get_stats(self) -> dict:
        """获取消息总线统计信息"""
        return {
            "total_messages": len(self._history),
            "pending_queues": {name: len(msgs) for name, msgs in self._queues.items()},
        }
