"""
日志系统 — 统一的日志输出
所有模块使用 logging.getLogger(__name__) 获取logger
"""
from __future__ import annotations
import logging
import sys


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"


def setup_logging(level: int = logging.INFO) -> None:
    """初始化全局日志配置"""
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        stream=sys.stdout,
    )


def get_logger(name: str) -> logging.Logger:
    """获取命名logger"""
    return logging.getLogger(name)
