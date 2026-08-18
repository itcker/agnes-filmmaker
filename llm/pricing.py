"""Agnes 模型定价表（$/百万 token）与成本计算。

价格来源：https://wiki.agnes-ai.com （标准价；当前促销期文本为 $0）
新增模型时只需在 PRICING 中添加一行即可。
"""
from __future__ import annotations

# 价格单位：$/百万 token
PRICING: dict[str, dict[str, float]] = {
    # ── Agnes AI (Sapiens) ──
    "agnes-2.5-flash":      {"input": 0.03, "output": 0.15},
    "agnes-2.0-flash":      {"input": 0.03, "output": 0.15},
    "agnes-2.5-pro-alpha":  {"input": 3.00, "output": 15.00},
}


def calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """根据模型名和 token 数计算美元成本。

    匹配策略：
    1. 精确匹配 PRICING 表中的模型名
    2. 关键词模糊匹配（模型名包含定价 key）
    3. 未知模型返回 0.0

    返回金额单位：美元 (float)
    """
    if not model or prompt_tokens + completion_tokens == 0:
        return 0.0

    model_lower = model.lower()

    # 1) 精确匹配
    if model_lower in PRICING:
        price = PRICING[model_lower]
        return (prompt_tokens / 1_000_000) * price["input"] + \
               (completion_tokens / 1_000_000) * price["output"]

    # 2) 关键词模糊匹配（优先匹配更长的 key）
    for key in sorted(PRICING, key=len, reverse=True):
        if key in model_lower:
            price = PRICING[key]
            return (prompt_tokens / 1_000_000) * price["input"] + \
                   (completion_tokens / 1_000_000) * price["output"]

    # 3) 未知模型
    return 0.0
