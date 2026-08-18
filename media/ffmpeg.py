"""ffmpeg 路径查找与视频拼接。

优先级：config.media.ffmpeg_path → 系统 PATH → imageio-ffmpeg 内置二进制。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger("media.ffmpeg")


def find_ffmpeg(ffmpeg_path: str = "") -> Optional[str]:
    """返回可用的 ffmpeg 可执行路径，找不到返回 None。"""
    # 1) 配置显式路径
    if ffmpeg_path and os.path.isfile(ffmpeg_path):
        return ffmpeg_path
    # 2) 系统 PATH
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                                encoding="utf-8", errors="ignore", timeout=5)
        if result.returncode == 0:
            return "ffmpeg"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # 3) imageio-ffmpeg 内置
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except ImportError:
        pass
    return None


def concat_videos(clip_paths: list[str], out_path: str | Path,
                  ffmpeg_path: str = "", timeout: int = 600) -> Optional[str]:
    """用 ffmpeg concat demuxer 按顺序拼接视频到 out_path。

    先尝试流拷贝（-c copy），失败则回退重编码（libx264/aac）。
    成功返回输出路径，失败返回 None。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg(ffmpeg_path)
    if not ffmpeg:
        log.warning("未找到 ffmpeg，跳过视频拼接（可安装 ffmpeg 或在 config.media.ffmpeg_path 指定）")
        return None

    clips = [p for p in clip_paths if p and os.path.exists(p) and os.path.getsize(p) > 0]
    if not clips:
        log.warning("无可用视频片段，跳过拼接")
        return None
    if len(clips) == 1:
        # 仅一段：直接复制为目标
        shutil.copy2(clips[0], out_path)
        log.info("仅 1 段视频，直接复制为成片: %s", out_path.name)
        return str(out_path)

    # 在 ASCII 临时目录里操作，规避 ffmpeg（Windows 原生）对非 ASCII 路径（如中文项目名）的兼容问题。
    # 输入/输出都在临时目录完成后，再用 Python 把结果移到目标（可能是中文）路径。
    try:
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            safe_clips = []
            for i, p in enumerate(clips):
                dst = tdir / f"clip_{i:04d}.mp4"
                shutil.copy2(p, dst)
                safe_clips.append(str(dst.resolve()))  # 绝对路径，确保 ffmpeg 能找到
            list_file = tdir / "concat_list.txt"
            list_file.write_text(
                "\n".join(f"file '{Path(n).as_posix()}'" for n in safe_clips) + "\n", encoding="utf-8"
            )
            tmp_out = tdir / "merged.mp4"

            # 1) 流拷贝
            cmd = [ffmpeg, "-f", "concat", "-safe", "0", "-i", str(list_file),
                   "-c", "copy", "-y", str(tmp_out)]
            log.info("拼接 %d 段视频（流拷贝）...", len(clips))
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="ignore", timeout=timeout)
            if not (r.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 0):
                # 2) 回退重编码
                log.warning("流拷贝失败，尝试重编码: %s", (r.stderr or "")[:300])
                cmd2 = [ffmpeg, "-f", "concat", "-safe", "0", "-i", str(list_file),
                        "-c:v", "libx264", "-c:a", "aac", "-y", str(tmp_out)]
                r2 = subprocess.run(cmd2, capture_output=True, text=True,
                                    encoding="utf-8", errors="ignore", timeout=timeout)
                if not (r2.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 0):
                    log.error("拼接失败: %s", (r2.stderr or "")[:500])
                    return None

            # 移到目标路径（Python 处理 Unicode 路径无障碍）
            shutil.move(str(tmp_out), str(out_path))
            log.info("拼接成功: %s (%dKB)", out_path.name, out_path.stat().st_size // 1024)
            return str(out_path)
    except subprocess.TimeoutExpired:
        log.error("拼接超时")
        return None
    except Exception as e:
        log.error("拼接异常: %s", e)
        return None


def mix_bgm(video_path: str, bgm_path: str, out_path: str | Path,
            ffmpeg_path: str = "", volume: float = 0.22, timeout: int = 600) -> str:
    """给已拼接的成片混入背景音乐（BGM 循环到视频时长，音量降低）。

    成功返回 out_path；失败（无 ffmpeg / BGM 文件缺失 / 混音出错）返回原 video_path，
    不改变成片——即最坏退化为「无声成片」，保证成片始终可用。
    """
    out_path = Path(out_path)
    ffmpeg = find_ffmpeg(ffmpeg_path)
    if not ffmpeg or not os.path.isfile(bgm_path):
        return video_path
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "with_bgm.mp4"
            # -stream_loop -1 让 BGM 无限循环，-shortest 截到视频长度；视频流 copy 不重编码
            cmd = [ffmpeg, "-y", "-i", str(video_path),
                   "-stream_loop", "-1", "-i", str(bgm_path),
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                   "-filter:a", f"volume={volume}", "-shortest", str(tmp)]
            log.info("混入背景音乐: %s", Path(bgm_path).name)
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="ignore", timeout=timeout)
            if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                shutil.move(str(tmp), str(out_path))
                return str(out_path)
            log.warning("BGM 混入失败，保留无声成片: %s", (r.stderr or "")[:300])
            return video_path
    except Exception as e:
        log.warning("BGM 混入异常，保留无声成片: %s", e)
        return video_path
