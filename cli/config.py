"""
cli.config — 项目配置、LLM Provider 解析、LLM 客户端创建
"""
from __future__ import annotations

import os
from pathlib import Path

from utils.logger import get_logger

# 项目根目录（所有 cli 模块共享）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── .env 文件加载 ──
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:
                os.environ[key] = value

try:
    import yaml
except ImportError:
    yaml = None

# 默认项目配置（config.yaml 中未配置时使用）
DEFAULT_PROJECT_CONFIG = {
    "project_name": "功夫兔",
    "work_type": "AI漫剧",
    "episode_plan": "单集3分钟x8集",
    "core_tags": ["功夫", "成长", "热血"],
    "logline": "一只会武功的兔子在师父的指导下，从自大到觉醒，最终在武林大会上证明自己",
    "core_emotions": ["热血", "温情", "搞笑"],
    "invariants": ["主角必须保持忠诚善良", "师徒关系为核心", "功夫元素贯穿始终"],
    "palette": {
        "主色": "#C8F98A",
        "辅色1": "#BD6E1E",
        "辅色2": "#88800E",
    },
    "style_keywords": {
        "表现形式": "3D CG风格（梦工厂功夫熊猫路线）",
        "色彩范围": "高饱和度+深阴影+暖色调",
        "拍摄类型": "电影化运镜+低角度仰拍",
    },
    "characters": [],
    "worldview": "拟人化动物武林世界",
}


def load_config(config_path: str | Path | None = None) -> dict:
    """加载项目配置"""
    if yaml is None:
        log = get_logger("config")
        log.warning("pyyaml 未安装，跳过配置加载，使用空配置")
        return {}
    if config_path is None:
        config_path = PROJECT_ROOT / "config.yaml"
    config_path = Path(config_path)
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {}


def _resolve_provider(config: dict, model: str, explicit_provider: str = "") -> str:
    """本项目只使用 Agnes，固定返回 "agnes"。

    保留 model / explicit_provider 形参仅为向后兼容调用方。
    """
    return "agnes"


def create_llm_clients(config: dict) -> dict:
    """创建 Agnes LLM 客户端，返回 {"agnes": client}。"""
    from llm.llm_client import create_llm_client, LLMClient  # noqa: F811

    log = get_logger("llm")
    agnes_cfg = (config.get("llm_providers", {}) or {}).get("agnes", {}) or {}
    kwargs = {k: v for k, v in {
        "api_key": agnes_cfg.get("api_key", ""),
        "base_url": agnes_cfg.get("base_url", ""),
    }.items() if v}

    try:
        import openai  # noqa: F401  验证依赖可用
        client: LLMClient = create_llm_client("agnes", **kwargs)
        log.info("Agnes LLM 客户端初始化成功")
        return {"agnes": client}
    except ImportError as e:
        log.warning("openai 依赖缺失: %s — Agent 将使用模拟模式", e)
    except Exception as e:
        log.warning("Agnes LLM 客户端初始化失败: %s — Agent 将使用模拟模式", e)
    return {}
