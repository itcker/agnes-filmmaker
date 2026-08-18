"""图片/视频 prompt 内容安全清洗。

在提交 Agnes 图片/视频 API 前清洗 prompt，移除可能触发内容安全策略的关键词。
"""
from __future__ import annotations

# 内容安全敏感词列表
_CONTENT_POLICY_WORDS = [
    'violence', 'violent', 'bloody', 'blood', 'gore', 'murder', 'kill', 'killing',
    'weapon', 'gun', 'knife', 'sword', 'bomb', 'explosion', 'shoot', 'shooting',
    'nude', 'naked', 'sexual', 'porn', 'erotic', 'drug', 'alcohol abuse',
    'torture', 'suicide', 'self-harm', 'racist', 'discrimination',
    'wound', 'wounded', 'bleeding', 'corpse', 'death', 'dead body',
    'abuse', 'assault', 'battle', 'war', 'fight', 'fighting',
    '暴力', '血腥', '杀戮', '武器', '枪支', '色情', '毒品',
    '流血', '尸体', '撕咬', '皮开肉绽', '浑身是血', '血泊',
    '撕下一块皮肉', '咬住', '伤口', '鞭打', '虐待',
]

# 参考图描述中需要剥离的"设计图/三视图"类术语（避免视频里出现设定图）
_DESIGN_SHEET_TERMS = [
    'three views', 'three-view', 'character design sheet', 'design board',
    'front view', 'side view', 'back view', 'orthographic', 'design sheet',
    'multiple views', 'close-up detail', 'detail showcase',
]


def _clean_words(prompt: str) -> str:
    prompt_lower = prompt.lower()
    cleaned = prompt
    for word in _CONTENT_POLICY_WORDS:
        if word.lower() in prompt_lower:
            cleaned = cleaned.replace(word, '').replace(word.lower(), '').replace(word.title(), '')
    return ' '.join(cleaned.split())


def sanitize_video_prompt(prompt: str) -> str:
    """清洗视频 prompt，移除可能触发内容安全策略的关键词。"""
    return _clean_words(prompt)


def sanitize_image_prompt(prompt: str) -> str:
    """清洗图片 prompt，移除可能触发内容安全策略的关键词。"""
    return _clean_words(prompt)


def strip_design_sheet_terms(desc: str) -> str:
    """剥离参考图描述中的三视图/设计图术语（供视频 prompt 复用角色外观时使用）。"""
    cleaned = desc
    for term in _DESIGN_SHEET_TERMS:
        cleaned = cleaned.replace(term, '').replace(term.title(), '')
    return ' '.join(cleaned.split())
