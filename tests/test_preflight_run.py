"""开跑前体检（2026-11-14）。

这个工具不是为了"多一个命令"，而是为了拦住两类真实发生过的事故：

1. **起跑前的闸太松**：11-13 早上 `recover` 探测失败、锁 6 秒后被写回，
   而 `multi start` 照样点了开始 —— 那条链路里没有任何东西在问"真的能跑吗"。
2. **人凭记忆判断状态**：11-14 早上我把 09:04 看成 09:25，
   以为 `inflight=True` 卡了 20 分钟，连 stop 两次、kill 两个进程；
   实际上那一轮才跑 3 分钟（search 105s + 4 条发送 ≈ 13 分钟）。
   **过度干预比不管更糟**：每次强杀都在丢弃已经发出的请求。

所以检查项要能给出"具体该怎么办"，而不是一个红叉。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.tools import preflight_run as pf


def _make_db(path: Path, jobs: int, greeted: int) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute("create table jobs (job_id text primary key, city text, title text)")
    conn.execute(
        "create table actions (id integer primary key autoincrement, "
        "action_type text, target text, created_at text)"
    )
    for i in range(jobs):
        conn.execute("insert into jobs values (?, '淄博', '法务')", (f"job_{i}",))
    for i in range(greeted):
        conn.execute(
            "insert into actions (action_type, target, created_at) values ('greet', ?, '2026-11-14T01:00:00Z')",
            (f"job_{i}",),
        )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------- 供给


def test_supply_plenty_is_green(tmp_path):
    db = _make_db(tmp_path / "a.db", jobs=200, greeted=100)
    ok, detail = pf.check_supply(db, target=50)
    assert ok is True
    assert "未打 100 个" in detail


def test_supply_below_target_is_red(tmp_path):
    """未打岗位少于今日目标 → 跑到一半会没岗位，反复空搜才是风控来源。"""
    db = _make_db(tmp_path / "b.db", jobs=60, greeted=50)
    ok, detail = pf.check_supply(db, target=50)
    assert ok is False
    assert "会中途缺岗位" in detail


def test_supply_between_target_and_margin_warns(tmp_path):
    """刚好够但不宽松 → 只提醒，不拦。"""
    db = _make_db(tmp_path / "c.db", jobs=100, greeted=45)  # 剩 55，目标 50，余量线 60
    ok, detail = pf.check_supply(db, target=50)
    assert ok == pf.WARN.strip()
    assert "偏紧" in detail


def test_missing_db_is_red(tmp_path):
    ok, detail = pf.check_supply(tmp_path / "nope.db", target=50)
    assert ok is False
    assert "库不存在" in detail


# ---------------------------------------------------------------- 锁


def test_no_lock(tmp_path):
    ok, _ = pf.check_locks({"data_dir": str(tmp_path), "budget_dir": str(tmp_path)})
    assert ok is True


def test_lock_in_either_dir_is_red(tmp_path):
    data_dir = tmp_path / "d"
    data_dir.mkdir()
    (data_dir / "cdp_risk_lock.json").write_text("{}", encoding="utf-8")
    ok, detail = pf.check_locks({"data_dir": str(data_dir), "budget_dir": str(tmp_path / "b")})
    assert ok is False
    assert "风控锁仍在" in detail


# ---------------------------------------------------------------- 本轮时长


def test_inflight_under_30min_is_not_wedged(monkeypatch):
    """batch=4 的一轮要 10–13 分钟。用「几分钟没动」判断卡死会误杀。"""
    from datetime import datetime, timedelta

    started = (datetime.now() - timedelta(minutes=12)).isoformat(timespec="seconds")
    monkeypatch.setattr(
        pf, "_get_json", lambda url, timeout=6.0: {"running": True, "used": 3, "target": 50,
                                                   "inflight": True, "pass_started_at": started}
    )
    ok, detail = pf.check_service(8010)
    assert ok is True
    assert "已跑 12 分钟" in detail


def test_inflight_over_30min_is_wedged(monkeypatch):
    from datetime import datetime, timedelta

    started = (datetime.now() - timedelta(minutes=41)).isoformat(timespec="seconds")
    monkeypatch.setattr(
        pf, "_get_json", lambda url, timeout=6.0: {"running": True, "used": 3, "target": 50,
                                                   "inflight": True, "pass_started_at": started}
    )
    ok, detail = pf.check_service(8010)
    assert ok is False
    assert "真卡死" in detail


def test_service_down_is_red(monkeypatch):
    def boom(url, timeout=6.0):
        raise OSError("refused")

    monkeypatch.setattr(pf, "_get_json", boom)
    ok, detail = pf.check_service(8010)
    assert ok is False
    assert "不可达" in detail


def test_config_rejects_non_boss_adapter():
    """adapter 不是 boss 就会跑假数据 —— 看着正常，其实一条都没发。

    这两条是从已废弃的 start-jobpilot.ps1 搬过来的，它的注释写着"这个坑踩过 4 次了"。
    """
    cfg = {"adapter": "fake", "target": {"search_url": "https://x"}, "pacing": {}, "quota": {}}
    ok, detail = pf.check_config(cfg)
    assert ok is False
    assert "假数据" in detail


def test_config_rejects_empty_search_url():
    """search_url 为空会走 --city 触发 INVALID_PARAM，白烧一次请求。"""
    cfg = {"adapter": "boss", "target": {}, "pacing": {}, "quota": {"daily_greet_max": 50}}
    ok, detail = pf.check_config(cfg)
    assert ok is False
    assert "search_url" in detail
