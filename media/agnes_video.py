"""Agnes AI 视频生成客户端（agnes-video-v2.0，异步任务制）。

端点：
  - 创建任务：POST {base_url}/videos            → 返回 {task_id, video_id}
  - 取回(推荐)：GET  {base_url}/agnesapi?video_id=<video_id>[&model_name=...]
  - 取回(兼容)：GET  {base_url}/videos/<task_id>

参数约束：num_frames ≤ 441 且满足 8n+1（81/121/241/441...），frame_rate ∈ [1,60]。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

from .http import ApiError, request_json

log = logging.getLogger("media.video")


class AgnesVideoClient:
    DEFAULT_MODEL = "agnes-video-v2.0"

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = (
            api_key
            or os.environ.get("AGNES_KEY", "")
            or os.environ.get("AGNES_API_KEY", "")
        )
        self.base_url = (
            base_url or os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")
        ).rstrip("/")

    @property
    def _videos_url(self) -> str:
        return f"{self.base_url}/videos"

    @staticmethod
    def validate_frames(num_frames: int, frame_rate) -> tuple[int, float]:
        """校正 num_frames 到合法的 8n+1（≤441），frame_rate 到 [1,60]。返回 (nf, fr)。"""
        nf = int(num_frames)
        if nf > 441:
            nf = 441
        k = round((nf - 1) / 8)
        if k < 0:
            k = 0
        nf = 8 * k + 1
        if nf < 1:
            nf = 1
        if nf > 441:
            nf = 441
        fr = float(frame_rate)
        fr = max(1.0, min(60.0, fr))
        return nf, fr

    def create_task(
        self,
        prompt: str,
        image: Optional[str] = None,
        width: int = 1152,
        height: int = 768,
        num_frames: int = 121,
        frame_rate: int = 24,
        model: Optional[str] = None,
        negative_prompt: Optional[str] = None,
        seed: Optional[int] = None,
        extra_images: Optional[list] = None,
        mode: Optional[str] = None,
        timeout: int = 60,
    ) -> dict:
        """创建视频任务。返回 {"task_id", "video_id", "raw"}。"""
        nf, fr = self.validate_frames(num_frames, frame_rate)
        body: dict = {
            "model": model or self.DEFAULT_MODEL,
            "prompt": prompt,
            "width": width,
            "height": height,
            "num_frames": nf,
            "frame_rate": fr,
        }
        if image:
            body["image"] = image
        if negative_prompt:
            body["negative_prompt"] = negative_prompt
        if seed is not None:
            body["seed"] = seed
        extra: dict = {}
        if extra_images:
            extra["image"] = extra_images
        if mode:
            extra["mode"] = mode
        if extra:
            body["extra_body"] = extra

        data = request_json("POST", self._videos_url, self.api_key, body, timeout=timeout)
        return {
            "task_id": data.get("task_id") or data.get("id"),
            "video_id": data.get("video_id"),
            "raw": data,
        }

    def retrieve(
        self,
        video_id: Optional[str] = None,
        task_id: Optional[str] = None,
        model_name: Optional[str] = None,
        timeout: int = 15,
    ) -> dict:
        """取回任务结果。优先用 video_id（推荐端点），否则用 task_id（兼容端点）。"""
        if video_id:
            params = {"video_id": video_id}
            if model_name:
                params["model_name"] = model_name
            return request_json(
                "GET", f"{self.base_url}/agnesapi", self.api_key, params=params, timeout=timeout
            )
        if task_id:
            return request_json(
                "GET", f"{self._videos_url}/{task_id}", self.api_key, timeout=timeout
            )
        raise ValueError("retrieve 需要 video_id 或 task_id")

    @staticmethod
    def extract_video_url(result: dict) -> str:
        """从取回响应中稳健地提取最终视频 URL（兼容多种字段名）。"""
        url = (
            result.get("video_url")
            or result.get("url")
            or result.get("output_url")
            or result.get("video")
            or result.get("remixed_from_video_id")
            or ""
        )
        if not url and isinstance(result.get("data"), dict):
            d = result["data"]
            url = d.get("url", "") or d.get("video_url", "") or d.get("video", "")
        if not url and isinstance(result.get("data"), list) and result["data"]:
            d = result["data"][0]
            url = d.get("url", "") or d.get("video_url", "")
        if not url and isinstance(result.get("metadata"), dict):
            m = result["metadata"]
            url = m.get("video_url", "") or m.get("url", "") or m.get("output_url", "")
        return url

    def poll_until_done(
        self,
        video_id: Optional[str] = None,
        task_id: Optional[str] = None,
        interval: int = 10,
        max_polls: int = 120,
        on_progress: Optional[Callable[[str, int], None]] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """轮询直到任务完成/失败/超时。返回最终结果 dict（含 _video_url 与 _status）。"""
        for i in range(max_polls):
            try:
                result = self.retrieve(
                    video_id=video_id, task_id=task_id, model_name=model_name
                )
            except ApiError as e:
                log.warning("轮询取回失败: %s，%ds 后继续", e, interval)
                time.sleep(interval)
                continue

            status = result.get("status", "")
            progress = result.get("progress", 0)
            log.info("[轮询 %d/%d] status=%s progress=%s", i + 1, max_polls, status, progress)
            if on_progress:
                try:
                    on_progress(status, progress)
                except Exception:
                    pass

            if status == "completed":
                result["_video_url"] = self.extract_video_url(result)
                result["_status"] = "completed"
                return result
            if status == "failed":
                result["_status"] = "failed"
                return result
            time.sleep(interval)

        return {"_status": "timeout", "status": "timeout"}
