"""验收报告窗口过滤（2026-11-05 修）。

原来"风控类失败"这一段**完全不看 `--days`**：`--days 1` 也会把几天前那次
`ENVIRONMENT_RISK` 列出来。三天验收靠的就是"风控 0 条"，这种假阳性会让人
以为今天又撞了，白停一天。

这组用例只构造最小的 `actions` / `runs` 两张表，验证窗口口径。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.tools import pacing_report


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_db(path, *, greet_offsets_days, run_rows):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE actions (id INTEGER PRIMARY KEY, action_type TEXT, target TEXT,"
            " status TEXT, response_json TEXT, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE runs (run_id INTEGER PRIMARY KEY, kind TEXT, started_at TEXT,"
            " ended_at TEXT, stats_json TEXT, error TEXT)"
        )
        for index, days in enumerate(greet_offsets_days, start=1):
            conn.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?)",
                (
                    index,
                    "greet",
                    f"job{index}",
                    "ok",
                    json.dumps({"code": 0, "tpl": "s_01"}),
                    _iso(_now() - timedelta(days=days)),
                ),
            )
        for index, (days, error) in enumerate(run_rows, start=1):
            conn.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?)",
                (
                    index,
                    "direct_greet",
                    _iso(_now() - timedelta(days=days)),
                    _iso(_now() - timedelta(days=days)),
                    "{}",
                    error,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_risk_hits_outside_window_are_excluded(tmp_path):
    db = tmp_path / "r.db"
    _make_db(
        db,
        greet_offsets_days=[0],
        run_rows=[
            (5, "search 命中风控（ENVIRONMENT_RISK）：…"),
            (4, "search 命中风控（ENVIRONMENT_RISK）：…"),
            (0, None),
        ],
    )

    report = pacing_report.collect(db, days=1)

    assert report["risk_hits"] == [], "窗口外的旧风控命不该出现（否则三天验收必假阳性）"


def test_risk_hits_inside_window_are_reported(tmp_path):
    db = tmp_path / "r2.db"
    _make_db(
        db,
        greet_offsets_days=[0],
        run_rows=[(0, "search 命中风控（ENVIRONMENT_RISK）：…")],
    )

    report = pacing_report.collect(db, days=1)

    assert len(report["risk_hits"]) == 1


def test_render_flags_missing_risk_clearly(tmp_path):
    db = tmp_path / "r3.db"
    _make_db(db, greet_offsets_days=[0, 0], run_rows=[(9, "ENVIRONMENT_RISK")])

    text = pacing_report.render(pacing_report.collect(db, days=1))

    assert "风控类失败（验收要求：0 条）：" in text
    assert "  无" in text
