"""项目模板管理 — 加载、列出、应用模板"""
from __future__ import annotations
import json
from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def list_templates() -> list[dict]:
    """扫描 templates/ 目录，返回所有可用模板的元信息"""
    templates = []
    if not TEMPLATES_DIR.exists():
        return templates
    for d in sorted(TEMPLATES_DIR.iterdir()):
        if d.is_dir():
            meta_file = d / "meta.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    templates.append({
                        "key": meta.get("key", d.name),
                        "name": meta.get("name", d.name),
                        "description": meta.get("description", ""),
                        "default_config": meta.get("default_config", {}),
                        "has_prompts": (d / "prompts").is_dir() and any((d / "prompts").glob("*.md")),
                    })
                except Exception:
                    pass
    return templates


def load_template(template_key: str) -> dict | None:
    """加载指定模板的 meta.json + 所有 prompt 文件内容"""
    # 先按 key 匹配
    for d in TEMPLATES_DIR.iterdir():
        if d.is_dir():
            meta_file = d / "meta.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    if meta.get("key") == template_key or d.name == template_key:
                        # 加载 prompts
                        prompts = {}
                        prompts_dir = d / "prompts"
                        if prompts_dir.exists():
                            for f in sorted(prompts_dir.glob("*.md")):
                                prompts[f.name] = f.read_text(encoding="utf-8")
                        meta["_prompts"] = prompts
                        meta["_dir"] = str(d)
                        return meta
                except Exception:
                    pass
    return None


def apply_template(engine, template_key: str) -> dict:
    """
    把模板的 prompts 覆盖到各 agent，返回模板的 default_config 供 /new 使用

    Args:
        engine: WorkflowEngine 实例
        template_key: 模板 key（如 "default"）

    Returns:
        模板的 default_config dict
    """
    template = load_template(template_key)
    if not template:
        return {}

    prompts = template.get("_prompts", {})

    # 文件名 → agent role 的映射
    filename_to_role = {
        "01_director.md": "director",
        "02_screenwriter.md": "screenwriter",
        "03_art_director.md": "art_director",
        "04_cinematographer.md": "cinematographer",
        "05_storyboarder.md": "storyboarder",
        "06_asset_builder.md": "asset_builder",
        "07_sound_designer.md": "sound_designer",
        "08_qa_reviewer.md": "qa_reviewer",
        "09_video_renderer.md": "video_renderer",
        "10_post_editor.md": "post_editor",
        "11_dialogue_expert.md": "dialogue_expert",
    }

    from agents.base import AgentRole

    applied = 0
    for filename, content in prompts.items():
        role_key = filename_to_role.get(filename)
        if not role_key:
            continue
        try:
            role = AgentRole(role_key)
        except ValueError:
            continue
        agent = engine.agents.get(role)
        if agent:
            agent.system_prompt = content
            applied += 1

    return {
        "default_config": template.get("default_config", {}),
        "applied_prompts": applied,
        "template_name": template.get("name", template_key),
    }
