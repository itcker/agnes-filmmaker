"""Agnes AI 图片生成客户端（agnes-image-2.1-flash）。

端点：POST {base_url}/images/generations
- 文生图：{model, prompt, size[, return_base64]}
- 图生图：{model, prompt, size, extra_body:{image:[...], response_format}}
注意：response_format 必须放在 extra_body 内（见官方文档）。
"""
from __future__ import annotations

import os
import re
from typing import Optional

from .http import request_json


def _normalize_size(size) -> Optional[str]:
    """把 size 规范为 'WxH' 字符串（如 '1024x1024'）。非法/裸数字返回 None。"""
    if size is None:
        return None
    if isinstance(size, (int, float)):
        return None  # 裸数字不是合法的 WxH
    s = str(size).strip().lower()
    # 兼容 "1024*768" / "1024×768"
    s = s.replace("×", "x").replace("*", "x")
    if re.match(r"^\d+x\d+$", s):
        return s
    return None


class AgnesImageClient:
    DEFAULT_MODEL = "agnes-image-2.1-flash"
    DEFAULT_SIZE = "1024x1024"

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
    def _endpoint(self) -> str:
        return f"{self.base_url}/images/generations"

    def text2img(
        self,
        prompt: str,
        size: Optional[str] = None,
        model: Optional[str] = None,
        return_base64: bool = False,
        timeout: int = 300,
    ) -> dict:
        """文生图。返回 {"url", "b64_json", "raw"}。"""
        body: dict = {
            "model": model or self.DEFAULT_MODEL,
            "prompt": prompt,
            "size": _normalize_size(size) or self.DEFAULT_SIZE,
        }
        if return_base64:
            body["return_base64"] = True
        data = request_json("POST", self._endpoint, self.api_key, body, timeout=timeout)
        item = (data.get("data") or [{}])[0]
        return {
            "url": item.get("url"),
            "b64_json": item.get("b64_json"),
            "raw": data,
        }

    def img2img(
        self,
        prompt: str,
        image,
        size: Optional[str] = None,
        model: Optional[str] = None,
        response_format: str = "url",
        timeout: int = 300,
    ) -> dict:
        """图生图 / 图片编辑。image 为单个 URL/DataURI 或其列表。返回 {"url","b64_json","raw"}。"""
        images = image if isinstance(image, list) else [image]
        body = {
            "model": model or self.DEFAULT_MODEL,
            "prompt": prompt,
            "size": _normalize_size(size) or self.DEFAULT_SIZE,
            "extra_body": {
                "image": images,
                "response_format": response_format,
            },
        }
        data = request_json("POST", self._endpoint, self.api_key, body, timeout=timeout)
        item = (data.get("data") or [{}])[0]
        return {
            "url": item.get("url"),
            "b64_json": item.get("b64_json"),
            "raw": data,
        }
