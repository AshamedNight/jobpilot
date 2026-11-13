"""actions 表的幂等抢占（任务书 §5.3）。

幂等键格式（唯一真源：架构 §5）：

    greet:{job_id}                  ← 不是 uid。greet 的参数是 (security_id, job_id)，
                                       uid 要打招呼之后才有，用它做键会导致重复打招呼
    reply:{uid}:{platform_mid}      ← 同一条消息只回一次
    exchange:{uid}                  ← 同一个会话只发起一次交换

原则：**宁可漏发，不可重发。**
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.models import utcnow

VALID_STATUS: tuple[str, ...] = ("pending", "ok", "failed", "unconfirmed")


def greet_key(job_id: str) -> str:
    return f"greet:{job_id}"


def reply_key(uid: str, platform_mid: str) -> str:
    return f"reply:{uid}:{platform_mid}"


def exchange_key(uid: str) -> str:
    return f"exchange:{uid}"


def try_claim(
    conn: sqlite3.Connection,
    key: str,
    action_type: str,
    target: str,
) -> bool:
    """抢占一个动作。True = 可以执行；False = 已做过 / 正在做。

    实现是 **INSERT + 捕获 IntegrityError**，不做 SELECT-then-INSERT（两者之间有竞态）。
    `actions.idempotency_key` 的 UNIQUE 约束是最后一道防线。
    """
    try:
        conn.execute(
            "INSERT INTO actions (action_type, target, idempotency_key, status, created_at)"
            " VALUES (?, ?, ?, 'pending', ?)",
            (action_type, target, key, utcnow()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    return True


def finish_claim(
    conn: sqlite3.Connection,
    key: str,
    status: str,
    response: dict[str, Any] | None = None,
) -> None:
    """回填动作结果。status 只会是 pending/ok/failed/unconfirmed。"""
    if status not in VALID_STATUS:
        raise ValueError(f"非法 status {status!r}，可选：{VALID_STATUS}")
    conn.execute(
        "UPDATE actions SET status = ?, response_json = ? WHERE idempotency_key = ?",
        (status, json.dumps(response, ensure_ascii=False) if response is not None else None, key),
    )
    conn.commit()


def get_claim(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """查一条动作记录（排障 / 测试断言用）。"""
    row = conn.execute(
        "SELECT id, action_type, target, idempotency_key, status, response_json, created_at"
        " FROM actions WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    return dict(row) if row else None


def count_claims(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM actions").fetchone()
    return int(row["n"]) if row else 0
