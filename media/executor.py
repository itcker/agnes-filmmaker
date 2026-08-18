"""媒体执行层 — 把 Agent 产出的结构化计划转为真实图片/视频并拼接。

高层流程：
  generate_assets : 读 asset_manifest.json → 文生图 → 下载 → 锁定参考图
  generate_videos : 读 video_render_plan.json + storyboard.json → 逐镜头文生视频 → 下载
  merge_final     : 收集镜头视频 → ffmpeg 拼接为 final.mp4

JSON 来源优先级：<stem>.json → 从 <stem>.md 抽取 → 调 LLM 归一化（normalize_json）。
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from .ffmpeg import concat_videos, mix_bgm
from .http import download_file
from .sanitize import (
    sanitize_image_prompt,
    sanitize_video_prompt,
    strip_design_sheet_terms,
)

log = logging.getLogger("media.executor")


# ───────────────────────── 结构化数据加载（含 LLM 归一化兜底）─────────────────────────

_SCHEMAS = {
    "assets": '{"characters":[{"name","prompt","size"}],"scenes":[{"name","prompt","size"}],"props":[{"name","prompt","size"}]}',
    "storyboard": '{"shots":[{"shot_index","id","scene_desc","characters":[],"action","camera","dialogue","prompt_en","duration"}]}',
    "video_plan": '{"shots":[{"id","prompt","width","height","num_frames","frame_rate","mode","negative_prompt"}]}',
}


def normalize_json(text_client, model: str, text: str, kind: str) -> Optional[dict]:
    """安全网：用一次专注的 LLM 调用把创作分析文本转成严格 JSON。"""
    if text_client is None or kind not in _SCHEMAS:
        return None
    try:
        from llm.llm_client import LLMMessage
        from utils.json_block import extract_json_block

        sys_prompt = (
            "你是一个结构化数据抽取器。把给定的创作分析文本转换为严格的 JSON。"
            "只输出 JSON 对象，不要任何解释、标题或额外文字。"
        )
        user_prompt = (
            f"目标 schema（字段名必须完全一致）：{_SCHEMAS[kind]}\n\n"
            "规则：prompt / prompt_en 必须是英文；同一角色外观描述跨条目保持一致；"
            "缺失数值字段用合理默认（num_frames=121, frame_rate=24, width=1152, height=768）。"
            "只输出 JSON 对象。\n\n文本：\n" + text
        )
        resp = text_client.chat(
            [LLMMessage(role="system", content=sys_prompt),
             LLMMessage(role="user", content=user_prompt)],
            model=model, temperature=0.1, max_tokens=8192,
        )
        data = extract_json_block(resp.content)
        return data if isinstance(data, dict) else None
    except Exception as e:
        log.warning("normalize_json(%s) 失败: %s", kind, e)
        return None


def load_structured(project_dir, role_dir: str, stem: str,
                    text_client=None, model: str = "", kind: str = "",
                    required_keys: tuple = ()) -> Optional[dict]:
    """加载某 Agent 的结构化产出。

    顺序：.json → 从 .md 抽取 → LLM 归一化。
    若指定 required_keys 且加载结果缺少这些键，则触发 LLM 归一化重整结构。
    """
    base = Path(project_dir) / "output" / role_dir
    jf = base / f"{stem}.json"
    md = base / f"{stem}.md"

    def _shape_ok(d):
        return isinstance(d, dict) and all(k in d for k in required_keys)

    data: Optional[dict] = None
    if jf.exists():
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            data = None

    if data is None and md.exists():
        from utils.json_block import extract_json_block
        data = extract_json_block(md.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            try:
                jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

    # 结构不符（如 LLM 用了 asset_list 而非 characters/scenes/props）→ 归一化
    if not _shape_ok(data) and text_client is not None and kind:
        src = ""
        if md.exists():
            src = md.read_text(encoding="utf-8")
        elif isinstance(data, dict):
            src = json.dumps(data, ensure_ascii=False)
        if src:
            norm = normalize_json(text_client, model, src, kind)
            if _shape_ok(norm) or isinstance(norm, dict):
                data = norm
                try:
                    jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass

    return data if isinstance(data, dict) else None


# ───────────────────────── 视频 prompt 构建（嵌入一致性外观）─────────────────────────

def build_shot_video_prompt(shot: dict, references: dict) -> str:
    """根据镜头计划 + 锁定的参考图，构建视频生成 prompt。

    base = 镜头 prompt（视频渲染师）→ 附加角色/场景/道具外观一致性 → 强制自然场景开场。
    """
    prompt = shot.get("prompt") or shot.get("prompt_en") or shot.get("scene_desc") or ""

    dialogue = shot.get("dialogue", "")
    if dialogue:
        prompt = f'{prompt}. Dialogue in Chinese: "{dialogue}"'

    mentioned = shot.get("characters") or []
    char_descs, scene_descs, prop_descs = [], [], []
    for category, bucket in (("characters", char_descs), ("scenes", scene_descs), ("props", prop_descs)):
        refs = references.get(category, {}) or {}
        for name, info in refs.items():
            info = info or {}
            if category == "characters" and mentioned and name not in mentioned:
                continue
            desc = info.get("description", "") or info.get("desc", "")
            desc = strip_design_sheet_terms(desc)
            if not desc:
                continue
            if category == "characters":
                bucket.append(f"{name}: {desc}")
            else:
                bucket.append(desc)

    consistency = []
    if char_descs:
        consistency.append("Character appearance (MUST match exactly): " + "; ".join(char_descs))
    if prop_descs:
        consistency.append("Props: " + "; ".join(prop_descs))
    if scene_descs:
        consistency.append("Scene: " + "; ".join(scene_descs))
    if consistency:
        prompt = (
            f"{prompt}. {' | '.join(consistency)}. "
            "Use reference images ONLY for character appearance consistency, NOT as the starting frame."
        )

    prompt = (
        f"{prompt}. The video MUST begin directly with the described natural cinematic scene. "
        "Never show any design sheet, character layout, three-view orthographic, or reference board "
        "in the video. Start immediately with the actual story scene."
    )
    return sanitize_video_prompt(prompt)


# ───────────────────────── 资产图生成 ─────────────────────────

def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(name)).strip("_")[:40] or "asset"


def generate_assets(memory, project_dir, cfg: dict, image_client,
                    text_client=None, model: str = "") -> dict:
    """读 asset_manifest → 为每个角色/场景/道具文生图 → 下载 → 锁定参考图。"""
    manifest = load_structured(project_dir, "asset_builder", "asset_manifest",
                               text_client, model, "assets",
                               required_keys=("characters", "scenes"))
    if not manifest:
        return {"ok": False, "error": "未找到资产清单（asset_manifest）"}

    img_cfg = (cfg or {}).get("image", {}) or {}
    default_size = img_cfg.get("size", "1024x1024")
    images_dir = Path(project_dir) / "output" / "asset_builder" / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    index = {"characters": [], "scenes": [], "props": [], "failed": []}

    def gen_one(category: str, item: dict):
        # 兼容字段别名：name/scene_name/character_name；prompt/prompt_en/description
        name = str(
            item.get("name") or item.get("scene_name") or item.get("character_name") or "asset"
        ).strip() or "asset"
        prompt = sanitize_image_prompt(
            item.get("prompt") or item.get("prompt_en") or item.get("description") or ""
        )
        if not prompt:
            return None
        size = item.get("size") or default_size
        out = images_dir / f"{category}_{_safe_name(name)}.png"
        try:
            r = image_client.text2img(prompt, size=size, model=img_cfg.get("model"))
            url = r.get("url")
            if not url:
                return None
            p = download_file(url, out)
            if not p:
                return None
            if memory is not None:
                memory.lock_reference(category, name, {
                    "image_path": str(p), "description": prompt, "category": category,
                })
            return {"name": name, "category": category, "path": str(p), "prompt": prompt}
        except Exception as e:
            log.warning("生成 %s/%s 失败: %s", category, name, e)
            return None

    for category in ("characters", "scenes", "props"):
        for it in (manifest.get(category, []) or []):
            res = gen_one(category, it)
            if res:
                index[category].append(res)
            else:
                index["failed"].append({"category": category, "name": it.get("name", "")})

    out_index = Path(project_dir) / "output" / "asset_builder" / "media_assets.json"
    out_index.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(len(index[c]) for c in ("characters", "scenes", "props"))
    log.info("资产图生成完成: %d 张成功, %d 失败", total, len(index["failed"]))
    return {
        "ok": True, "images": total,
        "characters": len(index["characters"]),
        "scenes": len(index["scenes"]),
        "failed": index["failed"],
    }


# ───────────────────────── 镜头视频生成 ─────────────────────────

def _shot_id(shot: dict) -> str:
    sid = shot.get("id")
    if sid:
        return str(sid)
    return f"S{str(shot.get('shot_index', '')).zfill(2)}"


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', str(s))]


def _write_videos_manifest(path: Path, results: dict):
    ordered = [results[k] for k in sorted(results.keys(), key=_natural_key)]
    path.write_text(
        json.dumps({"shots": ordered}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _gen_shot(shot: dict, sb: dict, references: dict, video_client, vcfg: dict,
              videos_dir: Path) -> dict:
    """生成单个镜头视频（模块级，供批量与单镜头复用）。

    sb 为该镜头对应的分镜信息（由调用方解析，支持 id 精确匹配 + 序号兜底）。
    """
    sid = _shot_id(shot)
    merged = {**(sb or {}), **{k: v for k, v in shot.items() if v not in (None, "")}}
    prompt = build_shot_video_prompt(merged, references)
    out = videos_dir / f"{_safe_name(sid)}.mp4"
    try:
        task = video_client.create_task(
            prompt,
            width=int(shot.get("width") or vcfg.get("width", 1152)),
            height=int(shot.get("height") or vcfg.get("height", 768)),
            num_frames=int(shot.get("num_frames") or vcfg.get("num_frames", 121)),
            frame_rate=shot.get("frame_rate") or vcfg.get("frame_rate", 24),
            negative_prompt=shot.get("negative_prompt") or None,
        )
        vid = task.get("video_id") or task.get("task_id")
        res = video_client.poll_until_done(
            video_id=vid,
            interval=int(vcfg.get("poll_interval", 10)),
            max_polls=int(vcfg.get("max_polls", 120)),
        )
        status = res.get("_status") or res.get("status")
        rec = {"id": sid, "video_id": vid, "task_id": task.get("task_id"),
               "status": status, "local_file": None, "video_url": None}
        if status == "completed":
            url = res.get("_video_url") or video_client.extract_video_url(res)
            rec["video_url"] = url
            if url:
                p = download_file(url, out)
                rec["local_file"] = p
                if not p:
                    rec["status"] = "download_failed"
        return rec
    except Exception as e:
        return {"id": sid, "status": "error", "error": str(e)[:300]}


def generate_single_video(memory, project_dir, cfg: dict, video_client, shot_id: str,
                          text_client=None, model: str = "") -> dict:
    """生成指定单个镜头的视频（强制重生成），并更新 media_videos.json。"""
    plan = load_structured(project_dir, "video_renderer", "video_render_plan",
                           text_client, model, "video_plan",
                           required_keys=("shots",)) or {}
    shots = plan.get("shots", []) or []
    shot = next((s for s in shots if _shot_id(s) == shot_id), None)
    if not shot:
        return {"ok": False, "error": f"未找到镜头 {shot_id}"}

    storyboard = load_structured(project_dir, "storyboarder", "storyboard",
                                 text_client, model, "storyboard",
                                 required_keys=("shots",)) or {}
    sb_map = {_shot_id(s): s for s in (storyboard.get("shots", []) or [])}
    vcfg = (cfg or {}).get("video", {}) or {}
    references = memory.get_references() if memory else {}
    videos_dir = Path(project_dir) / "output" / "video_renderer" / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(project_dir) / "output" / "video_renderer" / "media_videos.json"

    results: dict[str, dict] = {}
    if manifest_path.exists():
        try:
            for r in json.loads(manifest_path.read_text(encoding="utf-8")).get("shots", []):
                if r.get("id"):
                    results[r["id"]] = r
        except Exception:
            pass
    results.pop(shot_id, None)  # 强制重生成

    # 分镜信息：优先 id 精确匹配，匹配不上则按序号兜底（挽救编号不一致导致的一致性丢失）
    sb_shots = storyboard.get("shots", []) or []
    sb = sb_map.get(shot_id)
    if sb is None:
        try:
            pidx = [_shot_id(s) for s in (plan.get("shots", []) or [])].index(shot_id)
            if 0 <= pidx < len(sb_shots):
                sb = sb_shots[pidx]
        except ValueError:
            pass

    rec = _gen_shot(shot, sb or {}, references, video_client, vcfg, videos_dir)
    results[rec["id"]] = rec
    _write_videos_manifest(manifest_path, results)
    log.info("单镜头 %s → %s", shot_id, rec.get("status"))
    return {"ok": rec.get("status") == "completed", "shot": rec}


def generate_videos(memory, project_dir, cfg: dict, video_client,
                    text_client=None, model: str = "", concurrency: Optional[int] = None) -> dict:
    """读 video_render_plan + storyboard → 逐镜头文生视频（并发）→ 下载。支持断点续跑。"""
    plan = load_structured(project_dir, "video_renderer", "video_render_plan",
                           text_client, model, "video_plan",
                           required_keys=("shots",))
    if not plan:
        return {"ok": False, "error": "未找到视频渲染计划（video_render_plan）"}
    shots = plan.get("shots", []) or []
    if not shots:
        return {"ok": False, "error": "视频渲染计划无镜头"}

    storyboard = load_structured(project_dir, "storyboarder", "storyboard",
                                 text_client, model, "storyboard",
                                 required_keys=("shots",)) or {}
    sb_map = {}
    for s in (storyboard.get("shots", []) or []):
        sb_map[_shot_id(s)] = s

    vcfg = (cfg or {}).get("video", {}) or {}
    concurrency = max(1, int(concurrency or vcfg.get("concurrency", 1)))
    references = memory.get_references() if memory else {}

    videos_dir = Path(project_dir) / "output" / "video_renderer" / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(project_dir) / "output" / "video_renderer" / "media_videos.json"

    # 断点续跑：加载已完成的镜头
    results: dict[str, dict] = {}
    if manifest_path.exists():
        try:
            prev = json.loads(manifest_path.read_text(encoding="utf-8"))
            for r in prev.get("shots", []):
                if r.get("id"):
                    results[r["id"]] = r
        except Exception:
            pass

    _sb_shots = storyboard.get("shots", []) or []

    def _resolve_sb(shot: dict) -> dict:
        sid = _shot_id(shot)
        sb = sb_map.get(sid)
        if sb is None:  # id 未匹配 → 按序号兜底
            try:
                pidx = shots.index(shot)
                if 0 <= pidx < len(_sb_shots):
                    sb = _sb_shots[pidx]
            except ValueError:
                pass
        return sb or {}

    def gen_one(shot: dict) -> dict:
        sid = _shot_id(shot)
        # 已完成且有本地文件 → 跳过（断点续跑）
        old = results.get(sid)
        if (old and old.get("status") == "completed"
                and old.get("local_file") and Path(old["local_file"]).exists()):
            return old
        return _gen_shot(shot, _resolve_sb(shot), references, video_client, vcfg, videos_dir)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(gen_one, s): s for s in shots}
        for fut in as_completed(futs):
            rec = fut.result()
            results[rec["id"]] = rec
            _write_videos_manifest(manifest_path, results)
            log.info("镜头 %s → %s%s", rec["id"], rec.get("status"),
                     "" if rec.get("status") == "completed" else f" ({rec.get('error','')})")

    _write_videos_manifest(manifest_path, results)
    ordered = [results[_shot_id(s)] for s in shots if _shot_id(s) in results]
    completed = sum(1 for r in ordered if r.get("status") == "completed")
    log.info("视频生成完成: %d/%d 镜头成功", completed, len(shots))
    return {"ok": True, "total": len(shots), "completed": completed, "shots": ordered}


# ───────────────────────── 拼接成片 ─────────────────────────

def merge_final(project_dir, cfg: dict, shot_results: Optional[list] = None) -> dict:
    """收集所有完成的镜头视频，按 id 顺序拼接为 output/post_editor/final.mp4。"""
    if shot_results is None:
        manifest_path = Path(project_dir) / "output" / "video_renderer" / "media_videos.json"
        if manifest_path.exists():
            try:
                shot_results = json.loads(manifest_path.read_text(encoding="utf-8")).get("shots", [])
            except Exception:
                shot_results = []
    shot_results = shot_results or []

    videos_dir = Path(project_dir) / "output" / "video_renderer" / "videos"

    clips = []
    for r in sorted(shot_results, key=lambda x: _natural_key(x.get("id", ""))):
        if r.get("status") == "completed":
            lf = r.get("local_file")
            # 优先用 local_file；若无则从 videos/ 目录按 shot id 查找（兜底 S1.mp4 / S1-01.mp4 两种格式）
            if not (lf and Path(lf).exists()):
                sid = r.get("id", "")
                for candidate in [f"{sid}.mp4", f"{sid}-01.mp4"]:
                    candidate_path = videos_dir / candidate
                    if candidate_path.exists() and candidate_path.stat().st_size > 0:
                        lf = str(candidate_path)
                        break
            if lf and Path(lf).exists():
                clips.append(lf)

    out = Path(project_dir) / "output" / "post_editor" / "final.mp4"
    p = concat_videos(clips, out, ffmpeg_path=(cfg or {}).get("ffmpeg_path", ""))
    # 可选背景音乐混入：config.media.bgm_path 指向一个音频文件时生效；失败自动回退无声成片
    bgm = (cfg or {}).get("bgm_path", "")
    if p and bgm and Path(bgm).exists():
        p = mix_bgm(p, bgm, out, ffmpeg_path=(cfg or {}).get("ffmpeg_path", ""))
    return {"ok": bool(p), "final": p, "clips": len(clips)}
