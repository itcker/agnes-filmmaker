"""把用户想法解析为项目配置（纯函数，不依赖 engine 实例）。

抽出独立模块，供 server.py 和 orchestrator 共用，避免循环 import。
"""
from __future__ import annotations
import json
from utils.logger import get_logger

log = get_logger("idea")


def parse_idea_to_config(idea: str, name: str, config: dict) -> dict:
    """用 LLM 把用户想法解析为项目配置。失败时回退默认配置。"""
    project_config = {
        "project_name": name,
        "work_type": "AI短片",
        "episode_plan": "单集1分钟×1集",
        "logline": idea,
        "core_emotions": ["好奇", "惊喜", "快乐"],
        "core_tags": [],
        "invariants": [],
        "characters": [],
        "worldview": "",
    }
    try:
        from cli.config import create_llm_clients, _resolve_provider
        clients = create_llm_clients(config)
        agent_cfg = (config.get("agents", {}) or {}).get("director", {}) or {}
        model = agent_cfg.get("model", "")
        provider = _resolve_provider(config, model, agent_cfg.get("provider", ""))
        client = clients.get(provider)
        if client:
            from llm.llm_client import LLMMessage
            parse_prompt = (
                f"用户想法：{idea}\n\n"
                "请将这个想法解析为JSON格式的项目配置，只输出JSON，不要解释：\n"
                '{"project_name":"简短名称","work_type":"类型","logline":"一句话故事",'
                '"core_emotions":["情绪"],"core_tags":["标签"],"invariants":["规则"],'
                '"characters":[{"name":"角色名","one_liner":"一句话描述","description":"详细描述"}],'
                '"worldview":"世界观"}'
            )
            response = client.chat([LLMMessage(role="user", content=parse_prompt)],
                                   model=model, temperature=0.3, max_tokens=1024)
            text = response.content.strip()
            if "```" in text:
                start = text.find("```")
                start = text.find("\n", start) + 1
                end = text.find("```", start)
                text = text[start:end].strip()
            parsed = json.loads(text)
            project_config.update(parsed)
            log.info("LLM解析成功: %s", parsed.get("project_name", ""))
    except Exception as e:
        log.warning("LLM解析失败，使用默认配置: %s", e)
    return project_config
