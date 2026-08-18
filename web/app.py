# -*- coding: utf-8 -*-
"""agnes-filmmaker Web 层（FastAPI）。

设计原则：引擎当服务，UI 只调不改。
  - import 现有 cli.team / workflow.engine / media.executor / shared.memory
  - 跑全流程在后台线程，状态/进度读引擎对象 + 磁盘文件（跨线程安全）
  - CLI 保持不变，作为功能回归基线
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from cli.config import PROJECT_ROOT, load_config
from cli.projects import (
    get_project_dir, load_projects, save_projects, sanitize_project_name,
    read_current_stage,
)
from cli.team import create_team, init_project, run_full_pipeline
from shared.memory import SharedMemory
from workflow.engine import WorkflowEngine, WorkflowStage, STAGE_AGENTS
from llm.llm_client import AgnesClient
from utils.versioning import diff_versions

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"

# 访问密码门：.env 设置 ACCESS_PWD 即开启；留空则完全开放（本地开发）
ACCESS_PWD = os.environ.get("ACCESS_PWD", "").strip()
SESSION_SECRET = secrets.token_hex(16)  # 每次启动随机，登录后写入 cookie

ALL_AGENTS = [
    "director", "screenwriter", "art_director", "cinematographer", "storyboarder",
    "asset_builder", "video_renderer", "sound_designer", "post_editor", "qa_reviewer",
    "dialogue_expert",
]

# 阶段中文名映射（供制作控制面板展示）
STAGE_LABELS = {
    "init": "项目初始化", "script": "剧本创作", "dialogue_review": "台词诊断",
    "art_direction": "美术定调", "shot_design": "镜头设计", "asset_build": "资产构建",
    "video_render": "视频渲染", "post_edit": "后期合成", "qa_review": "质控评审",
    "final": "最终交付",
}
# 流水线阶段顺序（不含 INIT/FINAL 之外的执行顺序，用于面板展示）
PIPELINE_STAGES = [
    "script", "dialogue_review", "art_direction", "shot_design", "asset_build",
    "video_render", "post_edit", "qa_review", "final",
]


# ───────────────────────── 配置 ─────────────────────────

def build_config() -> dict:
    """读取 config.yaml（若有），并补齐 Agnes + media 默认值。"""
    cfg = load_config() or {}
    cfg.setdefault("llm_providers", {}).setdefault("agnes", {})
    cfg.setdefault("agents", {
        n: {"model": "agnes-2.5-flash", "provider": "agnes", "temperature": 0.6, "max_tokens": 4096}
        for n in ALL_AGENTS
    })
    cfg.setdefault("media", {
        "enabled": True,
        "image": {"model": "agnes-image-2.1-flash", "size": "1024x1024"},
        "video": {"model": "agnes-video-v2.0", "num_frames": 121, "frame_rate": 24,
                  "concurrency": 1, "poll_interval": 10, "max_polls": 120},
        "ffmpeg_path": "",
        "bgm_path": "",
    })
    (cfg.get("media") or {}).setdefault("bgm_path", "")  # 可选：指向一个音频文件即可给成片铺背景音乐
    return cfg


# ───────────────────────── 想法 → 项目档案 ─────────────────────────

_IDEA_SYS = (
    "你是一个短片项目配置解析器。把用户的想法解析为 JSON 项目配置，只输出 JSON。格式：\n"
    '{"project_name":"2-6字名称","work_type":"AI短片","episode_plan":"单集约60秒x1集",'
    '"logline":"一句话故事","core_emotions":["情绪"],"core_tags":["标签"],'
    '"invariants":["规则"],"worldview":"世界观"}\n'
    "规则：project_name 简洁有记忆点；core_emotions 2-3个；若用户没指明时长默认单集约60秒。"
)


def idea_to_brief(idea: str) -> dict:
    """用 Agnes 把一句话想法解析为项目档案。失败则用兜底档案。"""
    try:
        from llm.llm_client import LLMMessage
        resp = AgnesClient().chat(
            [LLMMessage(role="system", content=_IDEA_SYS),
             LLMMessage(role="user", content=idea)],
            model="agnes-2.5-flash", temperature=0.3, max_tokens=1024,
        )
        text = resp.content.strip()
        if "```" in text:
            s = text.find("```"); s = text.find("\n", s) + 1
            e = text.find("```", s); text = text[s:e].strip()
        data = json.loads(text)
    except Exception:
        data = {}
    name = (data.get("project_name") or idea[:6] or "未命名").strip()
    return {
        "project_name": name,
        "work_type": data.get("work_type", "AI短片"),
        "episode_plan": data.get("episode_plan", "单集约60秒x1集"),
        "logline": data.get("logline") or idea,
        "core_emotions": data.get("core_emotions", []),
        "core_tags": data.get("core_tags", []),
        "invariants": data.get("invariants", []),
        "worldview": data.get("worldview") or idea,
        "characters": [],
        "palette": {}, "style_keywords": {},
    }


# ───────────────────────── 任务管理（后台线程）─────────────────────────

# ───────────────────────── prompt 内容合规改写（辅助创作表述符合平台内容规范）─────────────────────────

_SOFTEN_SYS = (
    "你是一个 AI 视频提示词合规改写助手。把给定英文 prompt 中过于直白的"
    "身体恐怖/血腥/暴力/死亡描写，改写为氛围化、梦幻、超现实的表达，"
    "使表述符合平台内容规范；保留原有场景节拍、主体与情绪。"
    "只输出改写后的英文 prompt，不要任何解释。"
)


def soften_prompt(text: str) -> str:
    """用 Agnes 对视频 prompt 做内容合规改写（保留创作意图、规范表述）。失败原样返回。"""
    if not text:
        return text
    try:
        from llm.llm_client import LLMMessage
        r = AgnesClient().chat(
            [LLMMessage(role="system", content=_SOFTEN_SYS),
             LLMMessage(role="user", content=text)],
            model="agnes-2.5-flash", temperature=0.4, max_tokens=400,
        )
        out = r.content.strip().strip("`").strip()
        return out or text
    except Exception:
        return text


def _read_shot_prompt(pdir: Path, shot_id: str) -> Optional[str]:
    plan_path = pdir / "output" / "video_renderer" / "video_render_plan.json"
    if not plan_path.exists():
        return None
    try:
        for s in json.loads(plan_path.read_text(encoding="utf-8")).get("shots", []):
            if str(s.get("id")) == str(shot_id):
                return s.get("prompt")
    except Exception:
        pass
    return None


def _write_shot_prompt(pdir: Path, shot_id: str, prompt: str):
    plan_path = pdir / "output" / "video_renderer" / "video_render_plan.json"
    if not plan_path.exists():
        return
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        for s in plan.get("shots", []):
            if str(s.get("id")) == str(shot_id):
                s["prompt"] = prompt
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ───────────────────────── 任务管理（后台线程）─────────────────────────

class JobManager:
    """管理每个项目的后台流水线任务。单机内存态。"""

    def __init__(self):
        self._lock = threading.RLock()  # 可重入：start() 内部会调 is_running()
        self.jobs: dict[str, dict] = {}
        self.last_result: dict[str, dict] = {}  # 最近一次后台任务结果（人工审查/单阶段执行）

    def is_running(self, pid: str) -> bool:
        with self._lock:
            j = self.jobs.get(pid)
            return bool(j and j.get("running"))

    def start(self, pid: str, engine: WorkflowEngine, max_revisions: int = 1) -> bool:
        with self._lock:
            if self.is_running(pid):
                return False
            job = {"running": True, "error": None, "engine": engine}
            self.jobs[pid] = job

        def _run():
            try:
                run_full_pipeline(engine, max_revisions=max_revisions)
            except Exception as e:
                job["error"] = str(e)[:500]
            finally:
                job["running"] = False

        threading.Thread(target=_run, daemon=True).start()
        return True

    def run_task(self, pid: str, engine: WorkflowEngine, fn, label: str) -> bool:
        """通用后台任务（单阶段执行/人工审查）。占用 is_running，结果存 last_result。

        与 start()（全流程）共用 is_running 互斥；fn(engine) 的返回值作为任务结果。
        """
        with self._lock:
            if self.is_running(pid):
                return False
            self.jobs[pid] = {"running": True, "error": None, "engine": engine}
            self.last_result[pid] = {"label": label, "done": False, "result": None, "error": None}

        def _run():
            try:
                res = fn(engine)
                self.last_result[pid] = {"label": label, "done": True, "result": res, "error": None}
            except Exception as e:
                self.last_result[pid] = {"label": label, "done": True, "result": None, "error": str(e)[:500]}
            finally:
                with self._lock:
                    j = self.jobs.get(pid)
                    if j:
                        j["running"] = False

        threading.Thread(target=_run, daemon=True).start()
        return True

    def status(self, pid: str) -> dict:
        with self._lock:
            j = self.jobs.get(pid)
            task = dict(self.last_result.get(pid) or {})
        if not j:
            return {"running": False, "error": None, "task": task}
        eng: Optional[WorkflowEngine] = j.get("engine")
        return {
            "running": j.get("running", False),
            "error": j.get("error"),
            "current_stage": getattr(eng, "current_stage", None).value if eng and getattr(eng, "current_stage", None) else None,
            "media": eng.media_summary() if eng else {},
            "task": task,
        }


jobs = JobManager()


# ───────────────────────── 辅助 ─────────────────────────

def _project_output(pid: str) -> Path:
    d = get_project_dir(pid) / "output"
    return d


def _safe_media_path(pid: str, rel: str) -> Path:
    """把相对路径解析到 projects/<id>/output/<rel>，防穿越。"""
    root = _project_output(pid).resolve()
    p = (root / rel).resolve()
    try:
        inside = os.path.commonpath([str(p), str(root)]) == str(root)
    except ValueError:  # Windows 下跨盘符等无法比较的情况
        inside = False
    if not inside:
        raise HTTPException(403, "非法路径")
    return p


def _murl(pid: str, rel: str) -> str:
    """构造媒体文件 URL（对 pid 与路径段做百分号编码，规避中文/括号在 URL 里的兼容问题）。"""
    from urllib.parse import quote
    return f"/media/{quote(pid, safe='')}/{quote(rel, safe='/')}"


# ───────────────────────── FastAPI ─────────────────────────

app = FastAPI(title="agnes-filmmaker", version="1.0")


# ── 密码门 ──
@app.middleware("http")
async def auth_gate(request, call_next):
    path = request.url.path
    if not ACCESS_PWD:                       # 未配置密码 → 完全开放
        return await call_next(request)
    if path.startswith("/api/auth/"):        # 登录接口本身放行
        return await call_next(request)
    authed = request.cookies.get("agnes_auth") == SESSION_SECRET
    if path in ("/", "/index.html"):         # 首页：已登录→工作台，未登录→登录页
        return FileResponse(str(STATIC_DIR / ("index.html" if authed else "login.html")))
    if not authed:
        return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
    return await call_next(request)


@app.post("/api/auth/login")
def api_login(payload: Optional[dict] = None):
    payload = payload or {}
    if not ACCESS_PWD:
        return JSONResponse({"ok": True, "open": True})
    if payload.get("password", "") == ACCESS_PWD:
        resp = JSONResponse({"ok": True})
        resp.set_cookie("agnes_auth", SESSION_SECRET, httponly=True, samesite="lax", max_age=30 * 86400)
        return resp
    return JSONResponse({"ok": False, "detail": "密码错误"}, status_code=401)


@app.get("/api/auth/check")
def api_auth_check():
    return {"required": bool(ACCESS_PWD)}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/projects")
def api_list_projects():
    out = []
    seen = set()

    def _add(name: str, info: dict):
        pid = sanitize_project_name(name)
        if pid in seen:
            return
        seen.add(pid)
        pdir = get_project_dir(name)
        brief = {}
        bf = pdir / "shared" / "project_brief.json"
        if bf.exists():
            try:
                brief = json.loads(bf.read_text(encoding="utf-8"))
            except Exception:
                pass
        final = pdir / "output" / "post_editor" / "final.mp4"
        out.append({
            "id": pid, "name": name,
            "logline": brief.get("logline", ""),
            "work_type": brief.get("work_type", ""),
            "created_at": info.get("created_at", ""),
            "has_film": final.exists(),
            "running": jobs.is_running(pid),
        })

    # 1) 注册表里的项目
    for name, info in load_projects().items():
        _add(name, info)
    # 2) 磁盘上存在但未注册的项目（兼容脚本/CLI 直接建的）
    proot = PROJECT_ROOT / "projects"
    if proot.exists():
        for d in sorted(proot.iterdir()):
            if d.is_dir() and (d / "shared" / "project_brief.json").exists():
                _add(d.name, {})
    return {"projects": out}


@app.post("/api/projects")
def api_create_project(payload: dict):
    """创建项目。body: {idea?:'一句话', brief?:{...}, name?:'强制名称'}"""
    brief = payload.get("brief")
    if not brief:
        idea = (payload.get("idea") or "").strip()
        if not idea:
            raise HTTPException(400, "需要 idea 或 brief")
        brief = idea_to_brief(idea)
    if payload.get("name"):
        brief["project_name"] = payload["name"]

    name = brief.get("project_name") or "未命名"
    pid = sanitize_project_name(name)
    pdir = get_project_dir(name)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "output").mkdir(exist_ok=True)
    (pdir / "shared").mkdir(exist_ok=True)

    SharedMemory(pdir).init_project_brief(brief)

    projects = load_projects()
    projects[name] = projects.get(name) or {}
    projects[name].setdefault("created_at", _now())
    save_projects(projects)
    return {"id": pid, "name": name, "brief": brief}


@app.post("/api/projects/{pid}/run")
def api_run_project(pid: str, payload: Optional[dict] = None):
    """启动全流程（后台线程）。"""
    payload = payload or {}
    name = _pid_to_name(pid)
    if not name:
        raise HTTPException(404, "项目不存在")
    pdir = get_project_dir(name)
    cfg = build_config()
    if payload.get("no_media"):
        cfg.setdefault("media", {})["enabled"] = False
    engine = create_team(pdir, cfg)
    if not engine.memory.get_project_brief():
        init_project(engine, {"project_name": name, "logline": ""})
    ok = jobs.start(pid, engine, max_revisions=int(payload.get("max_revisions", 1)))
    if not ok:
        raise HTTPException(409, "该项目已在运行")
    return {"id": pid, "started": True}


@app.get("/api/projects/{pid}/status")
def api_status(pid: str):
    st = jobs.status(pid)
    # 非运行项目也补上 media（从磁盘读），让前端能看到镜头/图片「计划总数 vs 已完成」与成片
    if "media" not in st:
        try:
            st["media"] = _engine_for(pid).media_summary()
        except Exception:
            st["media"] = {}
    return st


@app.get("/api/projects/{pid}/events")
def api_events(pid: str):
    """SSE：每 2 秒推送一次状态快照（阶段 + 媒体计数），直到任务结束。"""
    def _stream():
        # 先推一帧
        yield f"data: {json.dumps(jobs.status(pid), ensure_ascii=False)}\n\n"
        while True:
            time.sleep(2)
            st = jobs.status(pid)
            yield f"data: {json.dumps(st, ensure_ascii=False)}\n\n"
            if not st.get("running"):
                break
    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/projects/{pid}/artifacts")
def api_artifacts(pid: str):
    """列出项目产出：文本、资产图、镜头视频、成片。"""
    name = _pid_to_name(pid)
    if not name:
        raise HTTPException(404, "项目不存在")
    out = _project_output(pid)

    # 文本产出（每个 agent 的 .md）
    texts = []
    if out.exists():
        for d in sorted(out.iterdir()):
            if d.is_dir():
                for f in sorted(d.glob("*.md")):
                    texts.append({
                        "agent": d.name,
                        "file": f.name,
                        "url": _murl(pid, f"{d.name}/{f.name}"),
                        "size": f.stat().st_size,
                    })

    # 资产图
    images = []
    img_dir = out / "asset_builder" / "images"
    if img_dir.exists():
        for f in sorted(img_dir.glob("*.png")):
            images.append({"name": f.stem, "url": _murl(pid, f"asset_builder/images/{f.name}")})

    # 镜头视频
    videos = []
    mv = out / "video_renderer" / "media_videos.json"
    if mv.exists():
        try:
            for s in json.loads(mv.read_text(encoding="utf-8")).get("shots", []):
                local = s.get("local_file")
                fname = Path(local).name if local else None
                videos.append({
                    "id": s.get("id"),
                    "status": s.get("status"),
                    "url": _murl(pid, f"video_renderer/videos/{fname}") if fname else None,
                })
        except Exception:
            pass

    final = out / "post_editor" / "final.mp4"
    return {
        "texts": texts, "images": images, "videos": videos,
        "final": (_murl(pid, "post_editor/final.mp4") if final.exists() else None),
    }


@app.post("/api/projects/{pid}/shots/{shot_id}/regenerate")
def api_reroll_shot(pid: str, shot_id: str, payload: Optional[dict] = None):
    """重生成单个镜头视频。

    payload: {prompt?: '自定义英文prompt', soften?: true} —— soften 时先对原 prompt 做内容合规改写再生成。
    """
    from media import executor as ex
    from media.agnes_video import AgnesVideoClient
    payload = payload or {}
    name = _pid_to_name(pid)
    if not name:
        raise HTTPException(404, "项目不存在")
    if jobs.is_running(pid):
        raise HTTPException(409, "项目全流程运行中，请稍后")
    pdir = get_project_dir(name)
    cfg = build_config()

    # 决定用于该镜头的 prompt（优先自定义 > 柔化 > 不改）
    if payload.get("prompt"):
        _write_shot_prompt(pdir, shot_id, payload["prompt"])
    elif payload.get("soften"):
        orig = _read_shot_prompt(pdir, shot_id) or ""
        if orig:
            _write_shot_prompt(pdir, shot_id, soften_prompt(orig))

    def _run():
        try:
            ex.generate_single_video(
                SharedMemory(pdir), pdir, cfg, AgnesVideoClient(), str(shot_id),
                AgnesClient(), "agnes-2.5-flash",
            )
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()
    return {"id": pid, "shot": shot_id, "regenerating": True,
            "softened": bool(payload.get("soften"))}


@app.get("/media/{pid}/{path:path}")
def api_media(pid: str, path: str):
    p = _safe_media_path(pid, path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(p))


@app.delete("/api/projects/{pid}")
def api_delete_project(pid: str):
    import shutil
    name = _pid_to_name(pid)
    if not name:
        raise HTTPException(404, "项目不存在")
    pdir = get_project_dir(name)
    if pdir.exists():
        shutil.rmtree(pdir, ignore_errors=True)
    projects = load_projects()
    projects.pop(name, None)
    save_projects(projects)
    return {"deleted": pid}


# ── 工具 ──

def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat()


def _pid_to_name(pid: str) -> Optional[str]:
    """pid（清洗后的目录名）→ 原始项目名。优先注册表，兼容未注册的磁盘项目。"""
    for name in load_projects().keys():
        if sanitize_project_name(name) == pid:
            return name
    # 兼容未注册的磁盘项目：目录存在则用目录名
    if (PROJECT_ROOT / "projects" / pid).exists():
        return pid
    return None


def _engine_for(pid: str) -> WorkflowEngine:
    """为（通常非运行中的）项目现场构造一个 engine。

    create_team 幂等：基于磁盘 SharedMemory 重建状态，注册全部 agent + 媒体客户端，
    本身不调用 LLM（毫秒级）。供人工门禁/阶段控制等操作复用。
    """
    name = _pid_to_name(pid)
    if not name:
        raise HTTPException(404, "项目不存在")
    return create_team(get_project_dir(name), build_config())


def _str_to_stage(s: str) -> Optional[WorkflowStage]:
    """字符串 → WorkflowStage，非法返回 None。"""
    try:
        return WorkflowStage(s)
    except ValueError:
        return None


def _stage_diffs(pdir: Path, stage_enum: WorkflowStage) -> list:
    """收集某阶段各 Agent 产出 .md 的版本对比（当前 vs 上一版本）。"""
    comps = []
    out = pdir / "output"
    for role in STAGE_AGENTS.get(stage_enum, []):
        adir = out / role.value
        if not adir.exists():
            continue
        for md in sorted(adir.glob("*.md")):
            d = diff_versions(md)
            comps.append({"role": role.value, "file": md.name, "diff": d})
    return comps


# ───────────────────────── 节点图执行引擎（LibTV/ComfyUI 式工作台后端）─────────────────────────

def _topo_order(nodes: list) -> list:
    by_id = {n["id"]: n for n in nodes}
    order, state = [], {}
    def visit(nid):
        s = state.get(nid)
        if s == 2 or s == 1:
            return
        state[nid] = 1
        for inp in by_id.get(nid, {}).get("inputs", []):
            f = inp.get("from")
            if f in by_id:
                visit(f)
        state[nid] = 2
        order.append(nid)
    for n in nodes:
        visit(n["id"])
    return order


@app.post("/api/graph/run")
def api_graph_run(payload: dict):
    """执行节点图。payload: {project_id?, nodes:[{id,type,params,inputs:[{slot,from}]}]}
    节点类型: text(纯文本源) / agnes_text(LLM扩写) / image(文生图) / video(文生视频) / merge(拼接)
    返回 {results:{node_id:{type,ok,outputs,preview,error}}}"""
    from media.agnes_image import AgnesImageClient
    from media.agnes_video import AgnesVideoClient
    from media.sanitize import sanitize_image_prompt
    from media.ffmpeg import concat_videos
    from media.http import download_file

    name = _pid_to_name(payload.get("project_id") or "") or "graph_lab"
    pid = sanitize_project_name(name)
    gdir = get_project_dir(name) / "output" / "graph"
    gdir.mkdir(parents=True, exist_ok=True)

    nodes = payload.get("nodes", [])
    by_id = {n["id"]: n for n in nodes}
    order = _topo_order(nodes)

    img_c, vid_c, txt_c = AgnesImageClient(), AgnesVideoClient(), AgnesClient()
    results: dict[str, dict] = {}

    def _up(slot, node_inputs):
        """取上游某 slot 的值（从已算好的 results 里）"""
        for inp in node_inputs:
            if inp.get("slot") == slot or slot is None:
                src = results.get(inp.get("from"), {}).get("outputs", {})
                return src.get(inp.get("slot")) or src.get(inp.get("from")) or next(iter(src.values()), None)
        return None

    def _preview(fname: str) -> str:
        return _murl(pid, f"graph/{fname}")

    for nid in order:
        n = by_id[nid]
        t = n.get("type")
        params = n.get("params", {}) or {}
        ins = n.get("inputs", []) or []
        res = {"type": t, "ok": False, "outputs": {}, "preview": None, "error": None}
        try:
            if t == "text":
                res["outputs"] = {"text": params.get("text", "")}; res["ok"] = True
            elif t == "agnes_text":
                prompt = _up("text", ins) or params.get("text", "")
                out = txt_c.simple_chat(
                    "你是短片创意文案。把用户输入扩写成一段画面感强的英文视觉描述，直接输出英文。",
                    prompt, max_tokens=400)
                res["outputs"] = {"text": out}; res["ok"] = True
            elif t == "image":
                prompt = _up("text", ins) or params.get("prompt", "")
                r = img_c.text2img(sanitize_image_prompt(prompt), size=params.get("size", "1024x768"))
                url = r.get("url")
                local = download_file(url, gdir / f"{nid}.png") if url else None
                res["outputs"] = {"image_path": local, "prompt": prompt}
                res["preview"] = _preview(f"{nid}.png") if local else None
                res["ok"] = bool(local)
            elif t == "video":
                prompt = _up("text", ins) or params.get("prompt", "")
                task = vid_c.create_task(
                    prompt, num_frames=int(params.get("num_frames", 81)),
                    frame_rate=int(params.get("frame_rate", 24)),
                    width=int(params.get("width", 1152)), height=int(params.get("height", 768)))
                rr = vid_c.poll_until_done(video_id=task.get("video_id"), interval=10, max_polls=80)
                vurl = rr.get("_video_url") or vid_c.extract_video_url(rr)
                local = download_file(vurl, gdir / f"{nid}.mp4") if vurl else None
                res["outputs"] = {"video_path": local}
                res["preview"] = _preview(f"{nid}.mp4") if local else None
                res["ok"] = bool(local)
            elif t == "merge":
                clips = []
                for inp in ins:
                    src = results.get(inp.get("from"), {}).get("outputs", {})
                    vp = src.get("video_path")
                    if vp and Path(vp).exists():
                        clips.append(vp)
                outp = concat_videos(clips, gdir / f"{nid}.mp4")
                res["outputs"] = {"final": outp}
                res["preview"] = _preview(f"{nid}.mp4") if outp else None
                res["ok"] = bool(outp)
            else:
                res["error"] = f"未知节点类型: {t}"
        except Exception as e:
            res["error"] = f"{type(e).__name__}: {str(e)[:250]}"
        results[nid] = res
    return {"results": results}


# ───────────────────────── 制作控制 / 人工门禁（纯增量端点，不改现有）─────────────────────────

@app.get("/api/projects/{pid}/pipeline")
def api_pipeline(pid: str):
    """制作控制面板数据：9 阶段 + 当前阶段 + 各 agent 状态。"""
    name = _pid_to_name(pid)
    if not name:
        raise HTTPException(404, "项目不存在")
    pdir = get_project_dir(name)
    cur = read_current_stage(pdir) or "init"
    # agent 状态：优先读持久化状态文件（含上次运行各 agent 的真实状态）
    agents_state = {}
    sf = pdir / "output" / "workflow_status.json"
    if sf.exists():
        try:
            agents_state = json.loads(sf.read_text(encoding="utf-8")).get("agents", {}) or {}
        except Exception:
            agents_state = {}
    if not agents_state:  # 兜底：无状态文件时构造 engine 读默认状态（不调 LLM）
        try:
            agents_state = _engine_for(pid).get_status().get("agents", {}) or {}
        except HTTPException:
            raise
        except Exception:
            agents_state = {}
    stages = []
    for s in PIPELINE_STAGES:
        stage_enum = _str_to_stage(s)
        ags = []
        for r in (STAGE_AGENTS.get(stage_enum, []) if stage_enum else []):
            info = agents_state.get(r.value) or {}
            ags.append({"name": info.get("name", r.value), "role": r.value,
                        "status": info.get("status", "idle")})
        stages.append({"stage": s, "label": STAGE_LABELS.get(s, s), "agents": ags,
                       "is_current": s == cur})
    return {"current_stage": cur, "stages": stages, "is_running": jobs.is_running(pid)}


@app.post("/api/projects/{pid}/stages/{stage}/run")
def api_run_stage(pid: str, stage: str):
    """单阶段执行（后台）。复用 run_task 占用 is_running，与全流程互斥。"""
    stage_enum = _str_to_stage(stage)
    if not stage_enum:
        raise HTTPException(400, f"未知阶段: {stage}")
    if jobs.is_running(pid):
        raise HTTPException(409, "该项目正在运行中，请稍后")
    eng = _engine_for(pid)

    def _do(e: WorkflowEngine):
        res = e.run_stage_async(stage_enum)
        e.save_status()  # 持久化 current_stage，供 read_current_stage 读取
        return {
            "stage": res.get("stage", stage),
            "agents": res.get("agents", []),
            "errors": res.get("errors", []),
            "has_media": "media" in res,
        }

    if not jobs.run_task(pid, eng, _do, f"run_stage:{stage}"):
        raise HTTPException(409, "该项目正在运行中，请稍后")
    return {"id": pid, "stage": stage, "started": True}


@app.post("/api/projects/{pid}/stages/{stage}/gate-review")
def api_gate_review(pid: str, stage: str):
    """人工 Gate Review（后台）：AI 审查结论 + 版本 diff，结果存 last_result。"""
    stage_enum = _str_to_stage(stage)
    if not stage_enum:
        raise HTTPException(400, f"未知阶段: {stage}")
    if jobs.is_running(pid):
        raise HTTPException(409, "该项目正在运行中，请稍后")
    name = _pid_to_name(pid)
    pdir = get_project_dir(name)
    eng = _engine_for(pid)

    def _do(e: WorkflowEngine):
        review = e.gate_review(stage_enum)
        return {
            "stage": stage, "label": STAGE_LABELS.get(stage, stage),
            "passed": review.get("passed", False),
            "decision": review.get("decision", "skipped"),
            "issues": review.get("issues", []),
            "reviewer_output": (review.get("reviewer_output", "") or "")[:4000],
            "director_output": (review.get("director_output", "") or "")[:4000],
            "comparisons": _stage_diffs(pdir, stage_enum),
        }

    if not jobs.run_task(pid, eng, _do, f"gate_review:{stage}"):
        raise HTTPException(409, "该项目正在运行中，请稍后")
    return {"id": pid, "stage": stage, "started": True}


@app.post("/api/projects/{pid}/stages/{stage}/approve")
def api_approve_stage(pid: str, stage: str):
    """批准阶段并推进到下一阶段（同步）。"""
    stage_enum = _str_to_stage(stage)
    if not stage_enum:
        raise HTTPException(400, f"未知阶段: {stage}")
    if jobs.is_running(pid):
        raise HTTPException(409, "该项目正在运行中，请稍后")
    eng = _engine_for(pid)
    eng.current_stage = stage_enum
    eng.advance_stage()
    eng.save_status()
    return {"approved": stage, "next_stage": eng.current_stage.value}


@app.post("/api/projects/{pid}/rollback")
def api_rollback(pid: str, payload: Optional[dict] = None):
    """回退（同步）。body: {stage?: 指定回退阶段, reason?}；不传 stage 则自动推断目标。"""
    if jobs.is_running(pid):
        raise HTTPException(409, "该项目正在运行中，请稍后")
    payload = payload or {}
    eng = _engine_for(pid)
    prev = eng.current_stage.value
    target = _str_to_stage(payload.get("stage") or "")  # None 时由引擎自动推断
    eng.rollback_stage(target, payload.get("reason") or "用户手动回退")
    eng.save_status()
    return {"from": prev, "to": eng.current_stage.value}


@app.get("/api/projects/{pid}/stages/{stage}/diff")
def api_stage_diff(pid: str, stage: str):
    """某阶段的版本对比（当前 vs 上一版本）。"""
    stage_enum = _str_to_stage(stage)
    if not stage_enum:
        raise HTTPException(400, f"未知阶段: {stage}")
    name = _pid_to_name(pid)
    if not name:
        raise HTTPException(404, "项目不存在")
    return {"stage": stage, "label": STAGE_LABELS.get(stage, stage),
            "comparisons": _stage_diffs(get_project_dir(name), stage_enum)}


@app.post("/api/projects/{pid}/video_render/continue")
def api_continue_videos(pid: str):
    """断点续跑视频生成：读现有 video_render_plan，跳过已完成镜头，只补齐剩余。
    不重跑任何 Agent / 不改动文本与已生成的视频。"""
    if jobs.is_running(pid):
        raise HTTPException(409, "该项目正在运行中，请稍后")
    name = _pid_to_name(pid)
    if not name:
        raise HTTPException(404, "项目不存在")
    cfg = build_config()
    eng = _engine_for(pid)

    def _do(e: WorkflowEngine):
        from media import executor as ex
        from media.agnes_video import AgnesVideoClient
        vid_c = (e.media_clients or {}).get("video") or AgnesVideoClient()
        txt_c = (e.media_clients or {}).get("text")
        res = ex.generate_videos(e.memory, e.project_dir, cfg, vid_c, txt_c, e.media_model)
        # 补齐后重新拼接成片（含所有已完成镜头）
        try:
            ex.merge_final(e.project_dir, cfg)
        except Exception:
            pass
        return {"completed": res.get("completed", 0), "total": res.get("total", 0),
                "error": res.get("error")}

    if not jobs.run_task(pid, eng, _do, "continue_videos"):
        raise HTTPException(409, "该项目正在运行中，请稍后")
    return {"id": pid, "started": True}


# 静态前端
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


def main():
    """启动 Web 服务：python -m web.app

    默认只监听 127.0.0.1:8000（安全默认，仅本机访问）。
    需要局域网/外网访问时设置 AGNES_WEB_HOST=0.0.0.0，并务必在 .env 配置 ACCESS_PWD。
    可用环境变量覆盖：AGNES_WEB_HOST / AGNES_WEB_PORT
    """
    import os
    import socket
    import uvicorn
    host = os.environ.get("AGNES_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("AGNES_WEB_PORT", "8000"))
    lan_ip = ""
    if host in ("0.0.0.0", "::"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
        except Exception:
            pass
    print("=" * 56)
    print(" agnes-filmmaker · 画布监制台已启动")
    print(f"   本机访问 : http://127.0.0.1:{port}")
    if lan_ip:
        print(f"   局域网访问: http://{lan_ip}:{port}")
        print("   ⚠ 已对外监听，请确认 .env 已设置 ACCESS_PWD")
    print("   Ctrl+C 退出")
    print("=" * 56)
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
