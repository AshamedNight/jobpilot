"""额度口径：**失败的 attempt 不占额度，也不算"新发"**（2026-11-08 修）。

现场：11-08 有 2 条 `AUTH_EXPIRED`（根本没发出去），但
① `count_actions_today` 把它们算进 `used` → 当天最多只能发 13 条真实的；
② `pacing_report` 把它们算进"每日新发" → 验收数字虚高。

两条都会让「每天 ≥15 条」的验收线永远失真，所以单独钉住。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app import models
from app.tools import pacing_report


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _add(conn: sqlite3.Connection, status: str, offset_min: int = 0) -> None:
    conn.execute(
        "INSERT INTO actions (action_type, target, status, response_json, created_at,"
        " idempotency_key) VALUES ('greet', ?, ?, ?, ?, ?)",
        (
            f"job-{status}-{offset_min}",
            status,
            json.dumps({"code": 0 if status == "ok" else None, "tpl": "s_01"}),
            _iso(datetime.now(timezone.utc) - timedelta(minutes=offset_min)),
            f"k-{status}-{offset_min}",
        ),
    )


def test_failed_actions_do_not_consume_quota(tmp_path):
    db = tmp_path / "q.db"
    conn = models.init_db(db)
    try:
        _add(conn, "ok", 30)
        _add(conn, "ok", 20)
        _add(conn, "failed", 10)  # 例如 AUTH_EXPIRED：平台侧根本没收到
        conn.commit()

        assert models.count_actions_today(conn, "greet", ok_only=True) == 2, "失败不该占额度"
        assert models.count_actions_today(conn, "greet") == 3, "默认口径（含失败）保持不变"
    finally:
        conn.close()


def test_report_counts_only_successful_sends(tmp_path):
    db = tmp_path / "r.db"
    conn = models.init_db(db)
    try:
        _add(conn, "ok", 60)
        _add(conn, "ok", 30)
        _add(conn, "failed", 20)
        conn.commit()
    finally:
        conn.close()

    report = pacing_report.collect(db, days=1)

    assert report["fresh"] == 2, "新发只算成功的"
    assert report["failed"] == 1, "失败要单独可见，否则'怎么少了'会查半天"


def test_report_interval_ignores_failed_gaps(tmp_path):
    """失败的时间点不能参与"相邻间隔"—— 它不是一个真实的发送动作。"""
    db = tmp_path / "g.db"
    conn = models.init_db(db)
    try:
        _add(conn, "ok", 30)
        _add(conn, "failed", 15)  # 若被算进去，间隔会被切成 2 段
        _add(conn, "ok", 0)
        conn.commit()
    finally:
        conn.close()

    report = pacing_report.collect(db, days=1)

    assert len(report["gaps"]) == 1
    assert report["gaps"][0] == 30 * 60
