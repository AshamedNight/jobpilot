"""API 层：只做参数校验 + 调 core/adapters，不写业务逻辑（任务书 §3）。

这里放两个共用依赖：`get_config` / `get_db`。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterator

from fastapi import Request

from app import models


def get_config(request: Request) -> dict[str, Any]:
    """启动时 load_config() 的结果（app.state.config）。页面额度数字从这里实时读。"""
    return request.app.state.config


def get_adapter(request: Request):
    """当前适配器（Phase 1 固定 FakeAdapter）。"""
    return request.app.state.adapter


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """每个请求开一条连接。SQLite + WAL 下这比共享连接安全。"""
    conn = models.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def is_paused(request: Request) -> bool:
    return bool(getattr(request.app.state, "paused", False))
