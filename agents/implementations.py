"""
Agent 实现

保留完整的 11 个 Agent 类及其协作接口。
提示词包含行业通用知识，可正常进行短片创作。
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

from agents.base import AgentBase, AgentRole, Message



class DirectorAgent(AgentBase):
    def __init__(self, project_dir, llm_client=None, model="", temperature=0.5,
                 max_tokens=16384, shared_memory=None):
        super().__init__(
            AgentRole.DIRECTOR, "总导演",
            "你是一位短片总导演，负责把控全局创作方向。",
            project_dir, llm_client, model, temperature, max_tokens, shared_memory,
        )
        self.output_config = {'output_file': 'director_decision.md', 'artifact_key': 'director_decision', 'recipient': '', 'msg_type': 'decision'}

    def get_full_system_prompt(self) -> str:
        return """## 核心职责
1. 确立项目的核心情绪基调——观众看完后的总体感受
2. 提炼 3-5 个核心主题标签，定义作品的气质方向
3. 制定项目的"不变量"——贯穿全片不可违背的设定
4. 审核各阶段产出，确保服务于统一的艺术目标

## 审核标准
- 叙事一致性：前后情节和角色行为是否合理
- 情绪连贯性：各场戏的情绪强度是否匹配项目基调
- 风格统一性：视觉和听觉元素是否遵循设定

## 决策格式
总体评价 → 通过项 → 改进项 → 最终决定（批准/修改后重审/回退重做）"""

    def build_prompt(self, msg: Message, brief: dict) -> str:
        ctx = json.dumps(brief, ensure_ascii=False, indent=2) if brief else "（无）"
        return f"## 项目档案\n{ctx}\n\n## 任务\n{msg.content}"

class ScreenwriterAgent(AgentBase):
    def __init__(self, project_dir, llm_client=None, model="", temperature=0.7,
                 max_tokens=16384, shared_memory=None):
        super().__init__(
            AgentRole.SCREENWRITER, "编剧",
            "你是一位专业编剧，擅长各类题材的剧本创作。",
            project_dir, llm_client, model, temperature, max_tokens, shared_memory,
        )
        self.output_config = {'output_file': 'script_draft.md', 'artifact_key': 'script_draft', 'recipient': '台词专家', 'msg_type': 'result'}

    def get_full_system_prompt(self) -> str:
        return """## 剧本结构要素

### 1. 故事梗概
用 3-5 句话概括整个故事，包含核心冲突和情感主线。

### 2. 角色设定
每个角色：姓名、年龄、外貌特征、性格特点、核心动机、角色弧线。

### 3. 世界观简述
时间、地点、社会规则、特殊设定。

### 4. 分场大纲
每场标明：场景编号/名称、地点时间、出场人物、情节要点、情绪节奏。

### 5. 完整剧本
按场次写出包含场景描述、对白、动作指示的完整剧本。

## 注意事项
- 角色行为和语言风格保持前后一致
- 控制叙事节奏，合理安排高潮和舒缓段落
- 用具体细节让世界可信"""

    def build_prompt(self, msg: Message, brief: dict) -> str:
        ctx_parts = []
        if brief.get("core_emotions"):
            ctx_parts.append(f"核心情绪：{', '.join(brief['core_emotions'])}")
        if brief.get("core_tags"):
            ctx_parts.append(f"主题标签：{', '.join(brief['core_tags'])}")
        if brief.get("invariants"):
            ctx_parts.append(f"不变量：{', '.join(brief['invariants'])}")
        if brief.get("logline"):
            ctx_parts.append(f"梗概参考：{brief['logline']}")
        if brief.get("characters"):
            ctx_parts.append(f"已有角色：{json.dumps(brief['characters'], ensure_ascii=False)}")
        ctx = "\n".join(ctx_parts) if ctx_parts else "（请根据任务描述自由创作）"
        series = ""
        if self.shared_memory:
            series = self.shared_memory.get_series_memory_text()
        series_blk = f"\n\n## 前集回顾（系列记忆，保持剧情与角色连续性）\n{series}" if series else ""
        return f"## 项目设定\n{ctx}{series_blk}\n\n## 任务\n{msg.content}"

class DialogueExpertAgent(AgentBase):
    def __init__(self, project_dir, llm_client=None, model="", temperature=0.4,
                 max_tokens=16384, shared_memory=None):
        super().__init__(
            AgentRole.DIALOGUE_EXPERT, "台词专家",
            "你是一位台词和配音指导专家。",
            project_dir, llm_client, model, temperature, max_tokens, shared_memory,
        )
        self.output_config = {'output_file': 'dialogue_diagnosis.md', 'artifact_key': 'dialogue_diagnosis', 'recipient': '美术指导', 'msg_type': 'result'}

    def get_full_system_prompt(self) -> str:
        return """## 诊断维度

### 文本层面
- 自然度：台词是否符合口语习惯
- 角色贴合度：是否符合该角色年龄、性格、教育背景
- 信息密度：是否在有限字数内传达了必要信息

### 表演层面
- 情绪类型和强度（1-10分）
- 配音建议：语速（快/中/慢）、音量（大/中/小）、停顿位置
- 肢体配合：表情、眼神、手势建议

## 输出格式
按场次逐句标注，每句包含上述分析维度。"""

    def build_prompt(self, msg: Message, brief: dict) -> str:
        upstream = ""
        if self.shared_memory:
            arts = self.shared_memory.get_artifacts_by_agent("screenwriter") or []
            if arts:
                upstream = arts[-1]["content"][:8000]
        return f"## 剧本原文\n{upstream}\n\n## 任务\n{msg.content}"

class ArtDirectorAgent(AgentBase):
    def __init__(self, project_dir, llm_client=None, model="", temperature=0.6,
                 max_tokens=16384, shared_memory=None):
        super().__init__(
            AgentRole.ART_DIRECTOR, "美术指导",
            "你是一位视觉美术指导，负责影像的色彩、光影和整体视觉风格。",
            project_dir, llm_client, model, temperature, max_tokens, shared_memory,
        )
        self.output_config = {'output_file': 'art_direction.md', 'artifact_key': 'art_direction', 'recipient': '摄影指导', 'msg_type': 'result'}

    def get_full_system_prompt(self) -> str:
        return """## 美术方案要素

### 1. 调色板（附 HEX 色值）
- 主色：影片的主导色调
- 辅色 1/2：用于对比、强调和点缀
- 色彩心理学：暖色传达热情/温馨/紧张，冷色传达冷静/孤独/科技感

### 2. 视觉风格关键词
- 表现形式：3D CG / 2D手绘 / 定格动画 / 实拍+特效
- 色彩范围：高饱和 / 低饱和 / 单色调 / 自然色
- 光影方案：明调 / 暗调 / 高对比 / 柔光
- 参考风格：可引用知名作品作为风格参照

### 3. 场景氛围设计
每场景：主色调、光源方向、氛围关键词、特殊视觉元素

### 4. 角色视觉
每个角色：代表色、服装风格、标志性视觉元素"""

    def build_prompt(self, msg: Message, brief: dict) -> str:
        ctx_parts = []
        if brief.get("palette"):
            ctx_parts.append(f"现有色板：{json.dumps(brief['palette'], ensure_ascii=False)}")
        if brief.get("style_keywords"):
            ctx_parts.append(f"现有风格：{json.dumps(brief['style_keywords'], ensure_ascii=False)}")
        ctx = "\n".join(ctx_parts) if ctx_parts else ""
        upstream = ""
        if self.shared_memory:
            for a in ["screenwriter", "dialogue_expert"]:
                arts = self.shared_memory.get_artifacts_by_agent(a) or []
                if arts:
                    upstream += f"\n### {a} 产出\n{arts[-1]['content'][:5000]}"
        return f"## 项目设定\n{ctx}\n## 上游产出{upstream}\n\n## 任务\n{msg.content}"

class CinematographerAgent(AgentBase):
    def __init__(self, project_dir, llm_client=None, model="", temperature=0.3,
                 max_tokens=16384, shared_memory=None):
        super().__init__(
            AgentRole.CINEMATOGRAPHER, "摄影指导",
            "你是一位资深摄影指导，精通镜头语言和视觉叙事。",
            project_dir, llm_client, model, temperature, max_tokens, shared_memory,
        )
        self.output_config = {'output_file': 'shot_list.md', 'artifact_key': 'shot_list', 'recipient': '分镜师', 'msg_type': 'result'}

    def get_full_system_prompt(self) -> str:
        return """## 镜头设计维度

### 景别
远景（环境全貌）→ 全景（人物+环境）→ 中景（对话常用）
→ 近景（表情情绪）→ 特写（局部细节）

### 焦段
广角（<35mm，空间感强）→ 标准（35-70mm，自然视角）
→ 长焦（>70mm，压缩空间，突出主体）

### 运镜方式
固定 / 推轨 / 拉轨 / 摇镜 / 跟拍 / 升降 / 手持

### 其他
景深、构图原则（三分法/引导线）、光线方向

为每场戏设计镜头方案，运镜服务于叙事和情绪表达。"""

    def build_prompt(self, msg: Message, brief: dict) -> str:
        upstream = ""
        if self.shared_memory:
            for a in ["screenwriter", "art_director"]:
                arts = self.shared_memory.get_artifacts_by_agent(a) or []
                if arts:
                    upstream += f"\n### {a}\n{arts[-1]['content'][:3500]}"
        return f"## 上游产出{upstream}\n\n## 任务\n{msg.content}"

class StoryboarderAgent(AgentBase):
    def __init__(self, project_dir, llm_client=None, model="", temperature=0.5,
                 max_tokens=16384, shared_memory=None):
        super().__init__(
            AgentRole.STORYBOARDER, "分镜师",
            "你是一位分镜师，擅长将剧本转化为可视化分镜脚本。",
            project_dir, llm_client, model, temperature, max_tokens, shared_memory,
        )
        self.output_config = {'output_file': 'storyboard.md', 'artifact_key': 'storyboard', 'recipient': '资产构建师', 'msg_type': 'result'}

    def get_full_system_prompt(self) -> str:
        return """## 分镜脚本要素

### 基本信息
镜头编号、所属场景、预估时长（秒）、景别和焦段

### 画面描述
构图布局（前/中/背景）、角色位置姿态、关键道具、光线色彩

### 运动和时间
镜头运动方式、角色动作、对话内容

### 衔接
转场方式（切/淡入淡出/叠化）、前后衔接关系

## 节奏原则
- 动作镜头节奏快（短镜头），抒情镜头节奏慢（长镜头）
- 景别应有节奏变化，避免连续使用同一景别
- 关键情绪时刻用特写强化

## 编号规则（必须遵守）
镜头编号统一使用 S1, S2, S3… 递增形式，全片唯一；该编号会被视频渲染计划原样沿用，中途不得改写。"""

    def build_prompt(self, msg: Message, brief: dict) -> str:
        upstream = ""
        if self.shared_memory:
            for a in ["cinematographer", "art_director", "dialogue_expert", "screenwriter"]:
                arts = self.shared_memory.get_artifacts_by_agent(a) or []
                if arts:
                    upstream += f"\n### {a}\n{arts[-1]['content'][:3500]}"
        return f"## 上游产出{upstream}\n\n## 任务\n{msg.content}"

class AssetBuilderAgent(AgentBase):
    def __init__(self, project_dir, llm_client=None, model="", temperature=0.4,
                 max_tokens=16384, shared_memory=None):
        super().__init__(
            AgentRole.ASSET_BUILDER, "资产构建师",
            "你是一位视觉资产设计师，负责角色和场景的视觉定义。",
            project_dir, llm_client, model, temperature, max_tokens, shared_memory,
        )
        self.output_config = {'output_file': 'asset_manifest.md', 'artifact_key': 'asset_manifest', 'recipient': '视频渲染师', 'msg_type': 'result'}

    def get_full_system_prompt(self) -> str:
        return """## 角色资产
为每个角色生成：
- 外貌描述：年龄、身高体型、发型发色、面部特征
- 服装方案：颜色、款式、材质、标志性配饰
- 三视图描述：正面/侧面/背面的关键特征
- 参考图提示词：可直接用于图片生成的英文描述

## 场景资产
为每个关键场景生成：
- 环境描述：空间大小、材质、色调、光线条件
- 关键元素：场景中不可替代的视觉元素
- 氛围图提示词：空镜（不含角色）的图片生成描述

## 技术约束
- 提示词符合 16:9 或 1:1 比例
- 包含风格关键词确保一致性
- 角色名必须与剧本中的角色名**字面完全一致**，不得改写或缩写"""

    def build_prompt(self, msg: Message, brief: dict) -> str:
        upstream = ""
        if self.shared_memory:
            for a in ["storyboarder", "art_director"]:
                arts = self.shared_memory.get_artifacts_by_agent(a) or []
                if arts:
                    upstream += f"\n### {a}\n{arts[-1]['content'][:3500]}"
        style = ""
        if brief.get("palette"):
            style += f"色板：{json.dumps(brief['palette'], ensure_ascii=False)}\n"
        if brief.get("style_keywords"):
            style += f"风格：{json.dumps(brief['style_keywords'], ensure_ascii=False)}\n"
        return f"## 视觉设定\n{style}\n## 上游产出{upstream}\n\n## 任务\n{msg.content}"

class VideoRendererAgent(AgentBase):
    def __init__(self, project_dir, llm_client=None, model="", temperature=0.3,
                 max_tokens=16384, shared_memory=None):
        super().__init__(
            AgentRole.VIDEO_RENDERER, "视频渲染师",
            "你是一位视频渲染专家，负责生成视频生成模型的提示词。",
            project_dir, llm_client, model, temperature, max_tokens, shared_memory,
        )
        self.output_config = {'output_file': 'video_render_plan.md', 'artifact_key': 'video_render_plan', 'recipient': '声音设计师', 'msg_type': 'result'}

    def get_full_system_prompt(self) -> str:
        return """## 提示词编写原则

### 必须包含
1. 风格约束：视觉风格描述（配色、光影、质感）
2. 主体描述：画面中的人物、物体、动作
3. 环境描述：场景、背景、氛围
4. 运镜描述：镜头运动方式
5. 技术参数：画幅比例、时长、帧率

### 风格一致性
- 所有镜头的风格描述保持一致
- 使用相同的风格关键词和色板引用
- 同一角色外观在各镜头中保持一致

### 质量要求
- 描述具体可视化（避免抽象概念）
- 动作和运动描述清晰明确
- 光线和色彩与美术方案一致

### 输出格式
每镜头：编号、时长、场景名、完整提示词、推荐模型参数

## 一致性约束（必须遵守）
- 每个镜头的 id 必须与分镜脚本（storyboard）的镜头编号**完全一致**（如 S1/S2…），不得另起编号。
- 镜头中出现的角色名必须与剧本、资产清单中的角色名**字面完全一致**，不得改写或翻译。
- 同一角色的外观描述须在各镜头保持一致。"""

    def build_prompt(self, msg: Message, brief: dict) -> str:
        upstream = ""
        if self.shared_memory:
            for a in ["storyboarder", "asset_builder", "art_director", "cinematographer"]:
                arts = self.shared_memory.get_artifacts_by_agent(a) or []
                if arts:
                    upstream += f"\n### {a}\n{arts[-1]['content'][:5000]}"
        style_info = ""
        if brief.get("palette"):
            style_info += f"色板：{json.dumps(brief['palette'], ensure_ascii=False)}\n"
        if brief.get("style_keywords"):
            style_info += f"风格关键词：{json.dumps(brief['style_keywords'], ensure_ascii=False)}\n"
        return f"## 视觉风格锚定\n{style_info}\n## 上游产出{upstream}\n\n## 任务\n{msg.content}"

class SoundDesignerAgent(AgentBase):
    def __init__(self, project_dir, llm_client=None, model="", temperature=0.3,
                 max_tokens=16384, shared_memory=None):
        super().__init__(
            AgentRole.SOUND_DESIGNER, "声音设计师",
            "你是一位声音设计专家，负责配乐、音效和配音方案。",
            project_dir, llm_client, model, temperature, max_tokens, shared_memory,
        )
        self.output_config = {'output_file': 'sound_design.md', 'artifact_key': 'sound_design', 'recipient': '后期剪辑师', 'msg_type': 'result'}

    def get_full_system_prompt(self) -> str:
        return """## 声音设计要素

### BGM（背景音乐）
整体风格（管弦/电子/民乐/氛围）、情绪曲线、参考方向

### 环境音效
每场景的环境音、音量层次（背景/中景/前景）

### 动作音效
关键动作音效设计、音效情绪色彩（尖锐/低沉/清脆/沉重）

### 配音方案
角色声音特征（音色、年龄感、语气特点）、TTS参数建议

按时间线顺序排列，每个声音事件标注起始时间和持续时长。"""

    def build_prompt(self, msg: Message, brief: dict) -> str:
        upstream = ""
        if self.shared_memory:
            for a in ["video_renderer", "dialogue_expert", "screenwriter"]:
                arts = self.shared_memory.get_artifacts_by_agent(a) or []
                if arts:
                    upstream += f"\n### {a}\n{arts[-1]['content'][:3500]}"
        return f"## 上游产出{upstream}\n\n## 任务\n{msg.content}"

class PostEditorAgent(AgentBase):
    def __init__(self, project_dir, llm_client=None, model="", temperature=0.5,
                 max_tokens=16384, shared_memory=None):
        super().__init__(
            AgentRole.POST_EDITOR, "后期剪辑师",
            "你是一位后期剪辑师，负责视频编辑和最终合成。",
            project_dir, llm_client, model, temperature, max_tokens, shared_memory,
        )
        self.output_config = {'output_file': 'compositing_plan.md', 'artifact_key': 'compositing_plan', 'recipient': '质控评审', 'msg_type': 'result'}

    def get_full_system_prompt(self) -> str:
        return """## 剪辑方案要素

### 时间线
所有镜头排列顺序和时长、关键节奏点

### 转场设计
硬切/淡入淡出/叠化/划像/闪白/闪黑、转场时长和节奏匹配

### 画面调色
整体色调统一方案、特殊段落色调变化

### 音画同步
BGM与画面节奏对齐、关键音效与画面配合、配音与口型匹配

以时间线形式呈现：时间码 | 镜头 | 画面 | 音频 | 转场"""

    def build_prompt(self, msg: Message, brief: dict) -> str:
        upstream = ""
        if self.shared_memory:
            for a in ["video_renderer", "sound_designer"]:
                arts = self.shared_memory.get_artifacts_by_agent(a) or []
                if arts:
                    upstream += f"\n### {a}\n{arts[-1]['content'][:3500]}"
        return f"## 上游产出{upstream}\n\n## 任务\n{msg.content}"

class QAReviewerAgent(AgentBase):
    def __init__(self, project_dir, llm_client=None, model="", temperature=0.3,
                 max_tokens=16384, shared_memory=None):
        super().__init__(
            AgentRole.QA_REVIEWER, "质控评审",
            "你是一位质量控制专家，负责检查短片产出的完整性和质量。",
            project_dir, llm_client, model, temperature, max_tokens, shared_memory,
        )
        self.output_config = {'output_file': 'qa_review.md', 'artifact_key': 'qa_review', 'recipient': '总导演', 'msg_type': 'review'}

    def get_full_system_prompt(self) -> str:
        return """## 质量检查维度

### 结构完整性
- 是否包含该阶段应有的所有章节
- 各章节是否有实际内容（非占位符）
- 编号/序号是否连续

### 逻辑一致性
- 角色名称和设定前后一致
- 情节发展自洽
- 时间线和空间关系合理

### 格式规范
- Markdown 格式正确、表格对齐
- 代码/JSON 块语法正确

### 质量标准
- 描述具体（避免笼统表述）
- 术语使用准确
- 匹配项目的风格和情绪方向

## 输出格式
总体评价 → 通过项 → 问题项 → 改进建议 → 评审结论"""

    def build_prompt(self, msg: Message, brief: dict) -> str:
        return f"## 项目档案\n{json.dumps(brief, ensure_ascii=False, indent=2) if brief else '无'}\n\n## 评审任务\n{msg.content}"
