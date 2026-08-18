"""HTTP 工具：带重试/退避的 JSON 请求与文件下载。

提供 JSON 请求的网关错误重试策略与流式文件下载保存。
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger("media.http")


class ApiError(RuntimeError):
    """Agnes API 调用错误（携带状态码与原始响应片段）。"""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"[{status_code}] {message}")


def request_json(
    method: str,
    url: str,
    api_key: str,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: int = 120,
    max_retries: int = 3,
) -> dict:
    """发起带 Bearer 鉴权的 JSON 请求，自动对 502/503/504、超时、连接错误退避重试。

    成功返回解析后的 JSON dict；最终失败抛出 ApiError。
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(
                method, url, headers=headers, json=json_body,
                params=params, timeout=timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            # 限流：视频生成限 1 次/分钟，等待后重试
            if resp.status_code == 429 and attempt < max_retries:
                wait = 60
                log.warning("限流 429，%ds 后重试 (%d/%d)", wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            if resp.status_code in (502, 503, 504) and attempt < max_retries:
                wait = 10 * (attempt + 1)
                log.warning("网关错误 %s，%ds 后重试 (%d/%d)", resp.status_code, wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            raise ApiError(resp.status_code, resp.text[:500])
        except requests.exceptions.Timeout:
            last_err = ApiError(0, "请求超时")
            if attempt < max_retries:
                wait = 10 * (attempt + 1)
                log.warning("请求超时，%ds 后重试 (%d/%d)", wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            raise last_err
        except requests.exceptions.ConnectionError as e:
            last_err = ApiError(0, f"连接失败: {e}")
            if attempt < max_retries:
                wait = 10 * (attempt + 1)
                log.warning("连接错误，%ds 后重试 (%d/%d)", wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            raise last_err
    raise last_err or ApiError(0, "未知错误")


def download_file(url: str, dest_path: str | Path, max_retries: int = 3,
                  timeout: int = 180, min_size: int = 1000) -> Optional[str]:
    """流式下载 URL 到 dest_path（重试 + 大小校验 + 原子写）。

    成功返回保存路径（str），失败返回 None。
    """
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(max_retries):
        tmp = dest_path.with_suffix(dest_path.suffix + f".part{uuid.uuid4().hex[:6]}")
        try:
            log.info("[下载] %s (尝试 %d/%d)", dest_path.name, attempt + 1, max_retries)
            resp = requests.get(url, timeout=timeout, stream=True)
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            size = tmp.stat().st_size
            if size < min_size:
                log.warning("[下载] 文件过小 (%d bytes)，疑似错误响应", size)
                tmp.unlink(missing_ok=True)
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return None
            tmp.replace(dest_path)
            log.info("[保存成功] %s (%dKB)", dest_path.name, size // 1024)
            return str(dest_path)
        except Exception as e:
            log.warning("[下载失败] %s 尝试 %d: %s", dest_path.name, attempt + 1, e)
            tmp.unlink(missing_ok=True)
            if attempt < max_retries - 1:
                time.sleep(3 * (attempt + 1))
    return None
