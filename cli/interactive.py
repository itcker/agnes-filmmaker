"""
cli.interactive — 交互式命令行模式（REPL）及所有 /command 处理
"""
from __future__ import annotations

import json
from datetime import datetime

from agents.base import AgentRole
from utils.templates import list_templates, apply_template
from workflow.engine import WorkflowEngine, WorkflowStage

from cli.config import PROJECT_ROOT, yaml
from cli.projects import load_projects, save_projects, get_project_dir
from cli.team import init_project, _parse_brief_with_llm

# ── 项目创建辅助 ──

def new_project_interactive(engine: WorkflowEngine):
    """交互式创建新项目 — 支持模板选择 + 大白话输入"""
    print("\n--- 新建项目 ---")

    # 第一步：模板选择
    templates = list_templates()
    selected_template = None
    if templates:
        print("\n选择项目模板（输入编号，回车跳过）：")
        for i, t in enumerate(templates, 1):
            prompts_mark = " [含prompt]" if t.get("has_prompts") else ""
            print(f"  {i}. {t['name']}{prompts_mark} — {t['description']}")
        print(f"  0. 跳过（不使用模板）")
        tpl_choice = input("模板: ").strip()
        if tpl_choice.isdigit() and 1 <= int(tpl_choice) <= len(templates):
            selected_template = templates[int(tpl_choice) - 1]
            print(f"已选择模板: {selected_template['name']}")
            # 应用模板 prompts 到 agents
            result = apply_template(engine, selected_template["key"])
            if result.get("applied_prompts"):
                print(f"已加载 {result['applied_prompts']} 个 Agent 的专属 Prompt")

    # 方式1：大白话输入
    print("\n用大白话描述你的想法（如：做一个赛博朋克猫的短片，孤独神秘的感觉，全程无对白）")
    print("或者输入 /manual 进入手动填写模式\n")
    idea = input("描述: ").strip()

    if not idea:
        print("已取消")
        return

    # 手动填写模式
    if idea == "/manual":
        tpl_defaults = selected_template.get("default_config", {}) if selected_template else {}
        return _new_project_manual(engine, tpl_defaults)

    # AI解析模式
    print("\n正在解析你的想法...")
    parsed = _parse_brief_with_llm(engine, idea)

    if not parsed:
        print("AI解析失败，切换到手动填写模式\n")
        return _new_project_manual(engine)

    # 展示解析结果
    name = parsed.get("project_name", "")
    work_type = parsed.get("work_type", "AI短片")
    episode = parsed.get("episode_plan", "单集1分钟x1集")
    logline = parsed.get("logline", "")
    emotions = parsed.get("core_emotions", [])
    tags = parsed.get("core_tags", [])
    invariants = parsed.get("invariants", [])
    worldview = parsed.get("worldview", "")

    print(f"\n{'─' * 40}")
    print(f"  项目：{name}")
    print(f"  类型：{work_type} | 时长：{episode}")
    print(f"  故事：{logline}")
    print(f"  情绪：{' / '.join(emotions)}")
    if tags:
        print(f"  标签：{' / '.join(tags)}")
    if invariants:
        print(f"  规则：{' / '.join(invariants)}")
    if worldview:
        print(f"  世界观：{worldview}")
    print(f"{'─' * 40}")

    print("\n[y]确认  [e]编辑  [n]取消")
    choice = input("选择: ").strip().lower()

    if choice == "n":
        print("已取消")
        return
    elif choice == "e":
        # 编辑模式：逐项确认/修改
        def ask_edit(label, current):
            val = input(f"  {label} [{current}]: ").strip()
            return val if val else current

        name = ask_edit("项目名称", name)
        work_type = ask_edit("类型", work_type)
        episode = ask_edit("时长", episode)
        logline = ask_edit("一句话故事", logline)
        emotions_str = ask_edit("情绪（逗号分隔）", ",".join(emotions))
        tags_str = ask_edit("标签（逗号分隔）", ",".join(tags))
        invariants_str = ask_edit("规则（逗号分隔）", ",".join(invariants))
        emotions = [e.strip() for e in emotions_str.split(",") if e.strip()]
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]
        invariants = [i.strip() for i in invariants_str.split(",") if i.strip()]
    # else: choice == "y" or empty, proceed

    # 构建项目配置（合并模板默认值）
    tpl_cfg = selected_template.get("default_config", {}) if selected_template else {}
    project_config = {
        "project_name": name,
        "work_type": work_type or tpl_cfg.get("work_type", "AI短片"),
        "episode_plan": episode or tpl_cfg.get("episode_plan", "单集1分钟x1集"),
        "logline": logline,
        "core_emotions": emotions,
        "core_tags": tags or tpl_cfg.get("core_tags", []),
        "invariants": invariants,
        "characters": [],
        "worldview": worldview,
        "palette": tpl_cfg.get("palette", {}),
        "style_keywords": tpl_cfg.get("style_keywords", {}),
    }

    _apply_new_project(engine, project_config)

def _new_project_manual(engine: WorkflowEngine, tpl_defaults: dict | None = None):
    """手动逐项填写模式，支持模板默认值"""
    if tpl_defaults is None:
        tpl_defaults = {}

    def ask(prompt, default=""):
        hint = f" [{default}]" if default else ""
        val = input(f"{prompt}{hint}: ").strip()
        if val == "-":
            return ""
        return val or default

    name = ask("项目名称", "")
    if not name:
        print("项目名称不能为空，已取消")
        return

    work_type = ask("类型", tpl_defaults.get("work_type", "AI短片"))
    episode = ask("时长/集数", tpl_defaults.get("episode_plan", "单集1分钟x1集"))
    logline = ask("一句话故事", "")
    emotions_raw = ask("核心情绪（逗号分隔）", "")
    invariants_raw = ask("不变量/规则（逗号分隔）", "")
    tags_raw = ask("关键词标签（逗号分隔）", tpl_defaults.get("core_tags") and ",".join(tpl_defaults["core_tags"]) or "")

    emotions = [e.strip() for e in emotions_raw.split(",") if e.strip()] if emotions_raw else []
    invariants = [i.strip() for i in invariants_raw.split(",") if i.strip()] if invariants_raw else []
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []

    project_config = {
        "project_name": name,
        "work_type": work_type,
        "episode_plan": episode,
        "logline": logline,
        "core_emotions": emotions,
        "core_tags": tags,
        "invariants": invariants,
        "characters": [],
        "worldview": "",
        "palette": tpl_defaults.get("palette", {}),
        "style_keywords": tpl_defaults.get("style_keywords", {}),
    }

    _apply_new_project(engine, project_config)

def _apply_new_project(engine: WorkflowEngine, project_config: dict):
    """应用新项目配置（创建项目目录，切换并初始化）"""
    name = project_config.get("project_name", "")

    # 创建项目目录
    project_dir = get_project_dir(name)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "output").mkdir(exist_ok=True)
    (project_dir / "shared").mkdir(exist_ok=True)

    # 切换引擎到新项目目录
    engine.switch_project(project_dir, name)

    # 重置所有Agent
    for agent in engine.agents.values():
        agent.reset()
        agent.inbox = []
        agent.outbox = []
        agent.status = "idle"

    # 初始化新项目
    init_project(engine, project_config)

    # 注册到 projects.json
    projects = load_projects()
    projects[name] = {"created_at": datetime.now().isoformat()}
    save_projects(projects)

    # 同步写入 config.yaml
    if yaml is not None:
        config_path = PROJECT_ROOT / "config.yaml"
        try:
            existing = {}
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    existing = yaml.safe_load(f) or {}
            existing["project"] = {
                "name": name,
                "type": project_config.get("work_type", ""),
                "episode_plan": project_config.get("episode_plan", ""),
                "core_tags": project_config.get("core_tags", []),
            }
            existing["emotions"] = project_config.get("core_emotions", [])
            existing["invariants"] = project_config.get("invariants", [])
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(existing, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        except Exception:
            pass

    print(f"\n项目 '{name}' 已创建！目录: projects/{name}/")
    print("输入 /run 开始生成剧本")

# ── 模型切换 ──

def switch_model_interactive(engine: WorkflowEngine, cmd: str):
    """交互式切换模型"""
    # 可选 Agnes 模型
    KNOWN_MODELS = {
        "1": ("agnes-2.5-flash", "Agnes 2.5 Flash（推荐）"),
        "2": ("agnes-2.0-flash", "Agnes 2.0 Flash"),
        "3": ("agnes-2.5-pro-alpha", "Agnes 2.5 Pro Alpha（推理）"),
    }

    # /model — 显示当前模型
    if cmd.strip() == "/model":
        current = {}
        for role, agent in engine.agents.items():
            m = getattr(agent, "model", "?")
            if m not in current:
                current[m] = []
            current[m].append(agent.name)

        print("\n当前模型：")
        for model, agents in current.items():
            print(f"  {model}")
            print(f"    └─ {', '.join(agents)}")

        print("\n可用模型：")
        for key, (model_id, name) in KNOWN_MODELS.items():
            print(f"  {key}. {name} ({model_id[:25]}...)")

        print("\n用法：")
        print("  /model all <编号或模型名>  — 全部Agent切换")
        print("  /model <agent> <编号或模型名>  — 单个Agent切换")
        print("  例: /model all 1        — 全部切到 Agnes 2.5 Flash")
        print("  例: /model director 3   — 只有总导演切到 Agnes 2.5 Pro Alpha")
        return

    # 解析命令
    parts = cmd.split(maxsplit=2)
    if len(parts) < 3:
        print("用法: /model all <模型>  或  /model <agent> <模型>")
        return

    target = parts[1]
    model_input = parts[2].strip()

    # 解析模型名
    if model_input in KNOWN_MODELS:
        new_model = KNOWN_MODELS[model_input][0]
        model_name = KNOWN_MODELS[model_input][1]
    else:
        new_model = model_input
        model_name = model_input

    # agent简称映射
    agent_short = {
        "director": "总导演", "screenwriter": "编剧", "art": "美术指导",
        "cinema": "摄影指导", "story": "分镜师", "asset": "资产构建师",
        "video": "视频渲染师", "sound": "声音设计师", "editor": "后期剪辑师",
        "qa": "质控评审", "dialogue": "台词专家",
    }

    if target == "all":
        for agent in engine.agents.values():
            agent.model = new_model
        print(f"全部Agent已切换到: {model_name}")
    else:
        found = False
        for role, agent in engine.agents.items():
            if role.value == target or agent.name == target or agent_short.get(target) == agent.name:
                agent.model = new_model
                print(f"{agent.name} 已切换到: {model_name}")
                found = True
                break
        if not found:
            print(f"未找到Agent: {target}")
            print(f"可用: {', '.join(agent_short.keys())}")

# ── 预览打印 ──

def _print_preview(data: dict):
    """打印单个Agent的结构化预览"""
    agent = data.get("agent", "")
    print(f"\n[{agent}] {data.get('file', '')}")

    if "scenes" in data:
        for s in data["scenes"]:
            dur = f" ({s['duration']})" if s.get("duration") else ""
            print(f"  {s['id']} {s['name']}{dur}")
            if s.get("desc"):
                print(f"    {s['desc']}")

    elif "shots" in data:
        print(f"  {len(data['shots'])} 个镜头")
        for s in data["shots"]:
            parts = [s["id"], s.get("scene", "")]
            if s.get("shot_type"):
                parts.append(s["shot_type"])
            if s.get("focal"):
                parts.append(s["focal"])
            if s.get("duration"):
                parts.append(s["duration"])
            print(f"  {' | '.join(parts)}")
            if s.get("prompt"):
                print(f"    提示词: {s['prompt'][:120]}...")

    elif "prompts" in data:
        print(f"  {len(data['prompts'])} 条图片提示词")
        for p in data["prompts"]:
            print(f"  {p['id']} {p.get('scene', '')} | {p.get('model', '')} | {p.get('ratio', '')}")
            if p.get("prompt"):
                print(f"    {p['prompt'][:150]}")

    elif "clips" in data:
        print(f"  {len(data['clips'])} 条视频提示词")
        for c in data["clips"]:
            dur = c.get("duration", "")
            cam = c.get("camera", "")
            print(f"  {c['id']} {c.get('scene', '')} | {c.get('model', '')} | {cam} | {dur}")
            if c.get("prompt"):
                print(f"    {c['prompt'][:150]}")

    elif "layers" in data:
        for layer in data["layers"]:
            print(f"  [{layer['name']}]")
            for item in layer.get("items", []):
                print(f"    - {item[:120]}")

# ── 主交互循环 ──

def interactive_mode(engine: WorkflowEngine):
    """
    交互模式 — 手动控制Agent对话
    """
    print("\n" + "=" * 60)
    print("agnes-filmmaker — 交互模式")
    print("=" * 60)
    print("\n可用命令：")
    print("  /new          - 新建项目（可选模板）")
    print("  /templates    - 列出可用模板")
    print("  /projects     - 列出所有项目")
    print("  /switch <名称> - 切换项目")
    print("  /run          - 运行当前阶段（支持重跑+自动质检）")
    print("  /rollback     - 回退到上一阶段")
    print("  /pipeline     - 运行完整流程")
    print("  /model        - 查看/切换模型")
    print("  /status       - 查看工作流状态")
    print("  /stage <name> - 切换到指定阶段")
    print("  /brief        - 查看项目档案")
    print("  /output       - 查看所有产出文件")
    print("  /preview      - 结构化预览产出（/preview <agent> 单个）")
    print("  /export       - 导出提示词到文件")
    print("  /generate-video <id|all> - 真实生成镜头视频（Agnes agnes-video-v2.0）")
    print("  /assets       - 列出已生成的角色/场景资产图")
    print("  /videos       - 列出镜头视频状态与成片")
    print("  /render       - 对当前阶段执行媒体生成（图/视频/拼接）")
    print("  /merge        - 用 ffmpeg 拼接所有镜头为 final.mp4")
    print("  /history      - 查看版本历史（/history <agent>）")
    print("  /diff <agent> - 对比当前版本和上一版本")
    print("  /restore <agent> <vN> - 恢复到指定版本")
    print("  /chat <agent> <msg> - 直接与Agent对话")
    print("  /review       - Gate Review审查")
    print("  /send <from> <to> <msg> - 发送消息")
    print("  /agents       - 查看所有Agent状态")

    print("  /trailer      - 生成30秒概念预告片大纲")
    print("  /quit         - 退出")

    role_names = {
        "director": AgentRole.DIRECTOR,
        "screenwriter": AgentRole.SCREENWRITER,
        "art": AgentRole.ART_DIRECTOR,
        "cinema": AgentRole.CINEMATOGRAPHER,
        "story": AgentRole.STORYBOARDER,
        "asset": AgentRole.ASSET_BUILDER,
        "video": AgentRole.VIDEO_RENDERER,
        "sound": AgentRole.SOUND_DESIGNER,
        "editor": AgentRole.POST_EDITOR,
        "qa": AgentRole.QA_REVIEWER,
        "dialogue": AgentRole.DIALOGUE_EXPERT,
    }

    while True:
        try:
            cmd = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not cmd:
            continue

        if cmd == "/quit":
            break
        elif cmd == "/status":
            status = engine.get_status()
            print(json.dumps(status, ensure_ascii=False, indent=2))
        elif cmd == "/agents":
            for role, agent in engine.agents.items():
                info = agent.get_status()
                llm_status = "有LLM" if agent.llm_client else "模拟模式"
                print(f"  {info['name']} ({info['role']}): {info['status']} [{llm_status}]")
        elif cmd.startswith("/stage"):
            parts = cmd.split()
            if len(parts) > 1:
                stage_name = parts[1]
                try:
                    stage = WorkflowStage(stage_name)
                    engine.advance_stage(stage)
                    print(f"已切换到: {stage.value}")
                except ValueError:
                    print(f"未知阶段: {stage_name}")
                    print(f"可用: {[s.value for s in WorkflowStage]}")
        elif cmd.startswith("/send"):
            parts = cmd.split(maxsplit=3)
            if len(parts) >= 4:
                from_name = parts[1]
                to_name = parts[2]
                content = parts[3]
                from_agent = None
                for role, agent in engine.agents.items():
                    if role.value == from_name or agent.name == from_name:
                        from_agent = agent
                        break
                if from_agent:
                    msg = from_agent.send(to_name, content, "task")
                    engine.bus.publish(msg)
                    print(f"消息已发送: {from_agent.name} → {to_name}")
                else:
                    print(f"未找到Agent: {from_name}")
            else:
                print("用法: /send <from> <to> <message>")
        elif cmd.startswith("/chat"):
            """直接与指定Agent对话，会调用LLM"""
            parts = cmd.split(maxsplit=2)
            if len(parts) >= 3:
                agent_name = parts[1]
                user_msg = parts[2]
                # 找到Agent
                target_agent = None
                for role, agent in engine.agents.items():
                    if role.value == agent_name or agent.name == agent_name:
                        target_agent = agent
                        break
                if target_agent:
                    print(f"\n[{target_agent.name}] 思考中...")
                    response = target_agent.call_llm(user_msg)
                    print(f"\n[{target_agent.name}]:\n{response}")
                else:
                    print(f"未找到Agent: {agent_name}")
                    print(f"可用: {[r.value for r in AgentRole]}")
            else:
                print("用法: /chat <agent_name> <message>")
                print("示例: /chat screenwriter 请写出第一场戏的剧本")
        elif cmd == "/review":
            review = engine.gate_review(engine.current_stage)
            status = "PASS" if review["passed"] else "FAIL"
            print(f"\nGate Review [{review['stage']}] — {status}")
            print(f"决策: {review['decision']}")
            if review.get("issues"):
                print(f"问题 ({len(review['issues'])} 项):")
                for issue in review["issues"]:
                    print(f"  - {issue}")
            else:
                print("无问题")
            if review.get("check_results"):
                print(f"\n检查结果:")
                for r in review["check_results"]:
                    mark = "PASS" if r.get("status") == "pass" else "FAIL" if r.get("status") == "fail" else "?"
                    print(f"  [{mark}] {r['item']}: {r.get('reason', '')[:80]}")
            if not review["passed"]:
                print("\n输入 /rollback 回退重做")
        elif cmd == "/run":
            result = engine.run_stage()
            agents_str = ", ".join(result.get("agents", []))
            msg_count = len(result.get("messages", []))
            print(f"阶段 [{result['stage']}] 完成 — Agents: {agents_str} — {msg_count} 条消息")
            # 自动执行 Gate Review
            review = engine.gate_review(engine.current_stage)
            if review["decision"] == "skipped":
                engine.advance_stage()
                print(f"已推进到: {engine.current_stage.value}")
            elif review["passed"]:
                print(f"Gate Review: PASS")
                engine.advance_stage()
                print(f"已推进到: {engine.current_stage.value}")
            else:
                print(f"Gate Review: FAIL")
                for issue in review.get("issues", []):
                    print(f"  - {issue}")
                print("输入 /review 查看详情，/rollback 回退重做")
        elif cmd == "/rollback":
            new_stage = engine.rollback_stage(reason="手动回退")
            print(f"已回退到: {new_stage.value}")
            print("输入 /run 重新执行该阶段")
        elif cmd == "/pipeline":
            from cli.team import run_full_pipeline
            run_full_pipeline(engine)
        elif cmd == "/new":
            new_project_interactive(engine)
        elif cmd == "/templates":
            templates = list_templates()
            if not templates:
                print("暂无模板。在 templates/ 目录下创建模板。")
            else:
                print("\n可用模板：")
                for i, t in enumerate(templates, 1):
                    prompts_mark = " [含prompt]" if t.get("has_prompts") else ""
                    print(f"  {i}. {t['name']} ({t['key']}){prompts_mark}")
                    print(f"     {t['description']}")
                    cfg = t.get("default_config", {})
                    if cfg.get("core_tags"):
                        print(f"     标签: {' / '.join(cfg['core_tags'])}")
                print("\n用法: /new 创建新项目时可选择模板")
        elif cmd == "/projects":
            projects = load_projects()
            if not projects:
                print("暂无项目。输入 /new 创建新项目")
            else:
                current = engine.project_name
                print("\n项目列表：")
                # 按系列分组显示
                series_shown = set()
                for name, info in projects.items():
                    if info.get("type") == "series":
                        print(f"\n  📂 {name}（系列）")
                        for ep_name, ep_info in projects.items():
                            if ep_info.get("series") == name:
                                marker = " ← 当前" if ep_name == current else ""
                                ep = ep_info.get("episode", "")
                                print(f"     └─ {ep} ({ep_name}){marker}")
                        series_shown.add(name)
                    elif "series" in info and info["series"]:
                        pass  # 已在系列分组中显示
                    else:
                        marker = " ← 当前" if name == current else ""
                        print(f"  📄 {name}{marker}")
        elif cmd.startswith("/switch"):
            parts = cmd.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                print("用法: /switch <项目名>")
                projects = load_projects()
                if projects:
                    print(f"可用项目: {', '.join(projects.keys())}")
            else:
                target_name = parts[1].strip()
                projects = load_projects()
                if target_name not in projects:
                    print(f"项目不存在: {target_name}")
                    if projects:
                        print(f"可用项目: {', '.join(projects.keys())}")
                else:
                    project_dir = get_project_dir(target_name)
                    engine.switch_project(project_dir, target_name)
                    # 重新初始化Agent状态
                    for agent in engine.agents.values():
                        agent.reset()
                        agent.inbox = []
                        agent.outbox = []
                        agent.status = "idle"
                    # 读取已有brief判断阶段
                    brief = engine.memory.get_project_brief()
                    if brief:
                        engine.current_stage = WorkflowStage.SCRIPT
                    print(f"已切换到项目: {target_name} (目录: projects/{target_name}/)")
        elif cmd.startswith("/model"):
            switch_model_interactive(engine, cmd)
        elif cmd == "/brief":
            brief = engine.memory.get_project_brief()
            print(json.dumps(brief, ensure_ascii=False, indent=2))
        elif cmd == "/output":
            output_dir = engine.project_dir / "output"
            if output_dir.exists():
                for d in sorted(output_dir.iterdir()):
                    if d.is_dir():
                        files = list(d.glob("*.md"))
                        if files:
                            print(f"\n  [{d.name}]")
                            for f in files:
                                print(f"    - {f.name}")
            else:
                print("暂无产出文件")
        elif cmd.startswith("/preview"):
            from utils.preview import preview_all, preview_agent
            parts = cmd.split(maxsplit=1)
            if len(parts) > 1 and parts[1].strip():
                data = preview_agent(engine.project_dir, parts[1].strip())
                if data:
                    _print_preview(data)
                else:
                    print(f"未找到: {parts[1].strip()}")
                    print("可用: screenwriter, storyboarder, image_generator, video_generator, sound_designer")
            else:
                results = preview_all(engine.project_dir)
                if not results:
                    print("暂无产出可预览")
                for data in results:
                    _print_preview(data)
                    print()
        elif cmd.startswith("/export"):
            from utils.preview import parse_image_prompts, parse_video_clips
            project_name = engine.memory.get_project_brief().get("project_name", "未命名")
            export_dir = engine.project_dir / "output" / "exported" / "prompts"
            export_dir.mkdir(parents=True, exist_ok=True)

            shots = {}
            # 解析图片提示词
            img_data = parse_image_prompts(engine.project_dir)
            for p in img_data.get("prompts", []):
                sid = p["id"]
                shots[sid] = shots.get(sid, {"id": sid, "scene": p["scene"]})
                shots[sid]["image_prompt"] = p["prompt"]
                shots[sid]["image_model"] = p["model"]
                shots[sid]["image_ratio"] = p["ratio"]
            # 解析视频提示词
            vid_data = parse_video_clips(engine.project_dir)
            for c in vid_data.get("clips", []):
                sid = c["id"]
                shots[sid] = shots.get(sid, {"id": sid, "scene": c["scene"]})
                shots[sid]["video_prompt"] = c["prompt"]
                shots[sid]["video_model"] = c["model"]
                shots[sid]["camera"] = c["camera"]
                shots[sid]["duration"] = c["duration"]

            shot_list = sorted(shots.values(), key=lambda s: s["id"])
            # 写出 shots.json
            json_path = export_dir.parent / "shots.json"
            json_path.write_text(
                json.dumps({"project": project_name, "shots": shot_list}, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            # 写出单镜头提示词文件
            for s in shot_list:
                sid = s["id"]
                if s.get("image_prompt"):
                    (export_dir / f"{sid}_image.txt").write_text(s["image_prompt"], encoding="utf-8")
                if s.get("video_prompt"):
                    (export_dir / f"{sid}_video.txt").write_text(s["video_prompt"], encoding="utf-8")

            print(f"已导出 {len(shot_list)} 个镜头到 output/exported/")
            print(f"  shots.json — 结构化数据")
            print(f"  prompts/ — 单镜头提示词文件")
        elif cmd.startswith("/history"):
            from utils.versioning import list_all_versions, list_versions
            parts = cmd.split(maxsplit=1)
            output_dir = engine.project_dir / "output"
            if len(parts) > 1 and parts[1].strip():
                agent_key = parts[1].strip()
                # 查找该agent的产出文件
                agent_dir = output_dir / agent_key
                if agent_dir.exists():
                    found = False
                    for f in sorted(agent_dir.glob("*.md")):
                        versions = list_versions(f)
                        if versions:
                            found = True
                            print(f"\n  {f.name} ({len(versions)} 个版本):")
                            for v in versions:
                                print(f"    {v['version']}  {v['modified']}  ({v['size']} bytes)")
                    if not found:
                        print(f"  {agent_key} 暂无版本记录")
                else:
                    print(f"未找到: {agent_key}")
            else:
                all_v = list_all_versions(output_dir)
                if not all_v:
                    print("暂无版本记录。重跑 /run 后自动生成版本。")
                else:
                    print("\n版本历史：")
                    for agent, files in all_v.items():
                        print(f"\n  [{agent}]")
                        for item in files:
                            print(f"    {item['file']} — {item['versions']} 个版本")
        elif cmd.startswith("/diff"):
            from utils.versioning import list_versions, diff_versions
            parts = cmd.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                print("用法: /diff <agent>")
                print("例: /diff screenwriter")
            else:
                agent_key = parts[1].strip()
                agent_dir = engine.project_dir / "output" / agent_key
                if not agent_dir.exists():
                    print(f"未找到: {agent_key}")
                else:
                    for f in sorted(agent_dir.glob("*.md")):
                        versions = list_versions(f)
                        if versions:
                            latest_v = versions[-1]["tag"]
                            result = diff_versions(f, v1=latest_v)
                            if result["diff_lines"]:
                                print(f"\n  {f.name} (当前 vs {latest_v}):")
                                for line in result["diff_lines"][:30]:
                                    print(f"    {line}")
                                if result["total_changes"] > 30:
                                    print(f"    ... 还有 {result['total_changes'] - 30} 行变更")
                            else:
                                print(f"\n  {f.name} — 无变更")
                            break
                    else:
                        print(f"  {agent_key} 暂无版本可对比")
        elif cmd.startswith("/restore"):
            from utils.versioning import list_versions, restore_version
            parts = cmd.split(maxsplit=2)
            if len(parts) < 3:
                print("用法: /restore <agent> <vN>")
                print("例: /restore screenwriter v1")
            else:
                agent_key = parts[1].strip()
                v_tag = parts[2].strip()
                agent_dir = engine.project_dir / "output" / agent_key
                if not agent_dir.exists():
                    print(f"未找到: {agent_key}")
                else:
                    restored = False
                    for f in sorted(agent_dir.glob("*.md")):
                        versions = list_versions(f)
                        if versions:
                            if restore_version(f, v_tag):
                                print(f"已恢复 {f.name} 到 {v_tag}")
                                restored = True
                            else:
                                print(f"未找到版本: {v_tag}")
                                print(f"可用: {', '.join(v['version'] for v in versions)}")
                            break
                    if not restored:
                        print(f"  {agent_key} 暂无版本可恢复")
        elif cmd.startswith("/generate-video"):
            parts = cmd.split(maxsplit=1)
            target = parts[1].strip() if len(parts) > 1 else "all"
            vid_client = engine.media_clients.get("video")
            if not vid_client:
                print("未启用视频生成。请在 config.yaml 设置 media.enabled=true 并配置 AGNES_KEY。")
                continue
            if target == "all":
                print("开始生成所有镜头视频（断点续跑，已完成的镜头会自动跳过）...")
                res = engine.run_media(WorkflowStage.VIDEO_RENDER)
                print(f"完成: {res.get('completed', 0)}/{res.get('total', 0)} 镜头成功")
                if res.get("error"):
                    print(f"错误: {res['error']}")
            else:
                from media import executor as ex
                print(f"生成单镜头 {target} ...")
                res = ex.generate_single_video(
                    engine.memory, engine.project_dir, engine.media_cfg,
                    vid_client, target, engine.media_clients.get("text"), engine.media_model)
                shot = res.get("shot", {})
                print(f"镜头 {target}: {shot.get('status')}")
                if shot.get("local_file"):
                    print(f"  已保存: {shot['local_file']}")
                elif res.get("error"):
                    print(f"  错误: {res['error']}")
        elif cmd == "/assets":
            idx_path = engine.project_dir / "output" / "asset_builder" / "media_assets.json"
            if not idx_path.exists():
                print("暂无资产图。请先运行到资产构建阶段（/pipeline 或 /run）。")
            else:
                idx = json.loads(idx_path.read_text(encoding="utf-8"))
                for cat in ("characters", "scenes", "props"):
                    items = idx.get(cat, [])
                    if items:
                        print(f"\n[{cat}] {len(items)} 张")
                        for it in items:
                            print(f"  {it.get('name')} -> {it.get('path')}")
                if idx.get("failed"):
                    print(f"\n失败: {len(idx['failed'])} 项")
        elif cmd == "/videos":
            mv_path = engine.project_dir / "output" / "video_renderer" / "media_videos.json"
            final = engine.project_dir / "output" / "post_editor" / "final.mp4"
            if not mv_path.exists():
                print("暂无镜头视频。请先运行到视频渲染阶段。")
            else:
                data = json.loads(mv_path.read_text(encoding="utf-8"))
                shots = data.get("shots", [])
                done = sum(1 for s in shots if s.get("status") == "completed")
                print(f"镜头视频: {done}/{len(shots)} 完成")
                for s in shots:
                    mark = "[OK]" if s.get("status") == "completed" else "[--]"
                    print(f"  {mark} {s.get('id')} {s.get('status')}")
            if final.exists():
                print(f"\n成片: {final}")
        elif cmd == "/render":
            res = engine.run_media(engine.current_stage)
            print(f"媒体生成 [{engine.current_stage.value}]:")
            print(json.dumps({k: v for k, v in res.items() if k != "shots"},
                             ensure_ascii=False, indent=2))
        elif cmd == "/merge":
            from media import executor as ex
            res = ex.merge_final(engine.project_dir, engine.media_cfg)
            if res.get("ok"):
                print(f"成片已生成: {res['final']} ({res['clips']} 段)")
            else:
                print(f"拼接失败（{res.get('clips', 0)} 段可用）。请确认已安装 ffmpeg。")
        elif cmd == "/trailer":
            brief = engine.memory.get_project_brief()
            if not brief:
                print("未找到项目档案，请先创建项目")
            else:
                llm_client = None
                llm_model = ""
                for agent in engine.agents.values():
                    if agent.llm_client:
                        llm_client = agent.llm_client
                        llm_model = getattr(agent, "model", "")
                        break
                if not llm_client:
                    print("未配置LLM，无法生成预告片大纲。")
                else:
                    print("\n正在生成30秒概念预告片大纲...")
                    try:
                        from llm.llm_client import LLMMessage
                        project_name = brief.get("project_name", "")
                        logline = brief.get("logline", "")
                        emotions = ", ".join(brief.get("core_emotions", []))
                        characters = brief.get("characters", [])
                        char_desc = "\n".join([f"- {c.get('name','')}: {c.get('one_liner','')}" for c in characters[:5]])

                        prompt = (
                            f"项目：{project_name}\n"
                            f"Logline：{logline}\n"
                            f"核心情绪：{emotions}\n"
                            f"角色：\n{char_desc}\n\n"
                            f"请将以上项目浓缩为一个30秒的概念预告片大纲，要求：\n"
                            f"1. 前5秒：钩子（悬念/视觉冲击/情绪冲突）\n"
                            f"2. 中间20秒：核心冲突+情绪递进+关键转折\n"
                            f"3. 最后5秒：悬念收束+片名推出\n"
                            f"4. 每个时间段标注：画面描述+声音设计+情绪目标\n"
                            f"5. 总字数控制在300字以内"
                        )
                        messages = [
                            LLMMessage(role="user", content=prompt),
                        ]
                        response = llm_client.chat(messages, model=llm_model, temperature=0.5, max_tokens=1024)
                        print(f"\n{response.content}")
                    except Exception as e:
                        print(f"生成失败: {e}")
        else:
            print(f"未知命令: {cmd}")
