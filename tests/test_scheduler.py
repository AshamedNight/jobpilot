"""调度器测试（P2-E）。

锁三件事：
1. 纯函数：`parse_sweep_times` / `next_sweep` / `followup_due` 的边界行为；
2. 集成：`schedule.enabled: true` 时 lifespan 真的把后台任务挂起来、shutdown 能干净退出；
3. 串行执行：上一轮没跑完时下一轮不会开始（真机一轮 1~2 分钟，重叠必撞上游预算）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time

from fastapi.testclient import TestClient

from app.main import create_app
from app.scheduler import followup_due, next_sweep, parse_sweep_times, run_scheduler


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def test_parse_sweep_times_filters_invalid():
    times = parse_sweep_times(["09:30", " garbage ", "20:30", None, 123])
    assert times == [time(9, 30), time(20, 30)]


def test_parse_sweep_times_empty():
    assert parse_sweep_times(None) == []
    assert parse_sweep_times([]) == []


def test_next_sweep_picks_nearest_future_today():
    now = datetime(2026, 10, 28, 10, 0)
    times = parse_sweep_times(["09:30", "20:30"])
    assert next_sweep(now, times) == datetime(2026, 10, 28, 20, 30)


def test_next_sweep_rolls_to_tomorrow_when_all_passed():
    now = datetime(2026, 10, 28, 21, 0)
    times = parse_sweep_times(["09:30", "20:30"])
    assert next_sweep(now, times) == datetime(2026, 10, 29, 9, 30)


def test_next_sweep_empty_times_returns_none():
    assert next_sweep(datetime(2026, 10, 28, 10, 0), []) is None


def test_followup_due_rules():
    assert followup_due(None, 20, 1000.0) is True          # 从未跑过 → 到期
    assert followup_due(1000.0, 20, 1000.0 + 19 * 60) is False
    assert followup_due(1000.0, 20, 1000.0 + 20 * 60) is True
    assert followup_due(None, 0, 1000.0) is False          # 间隔 0 = 关闭 followup


# --------------------------------------------------------------------------- #
# 集成：lifespan 挂载与干净退出
# --------------------------------------------------------------------------- #
def test_scheduler_starts_and_stops_with_lifespan(tmp_path):
    """`schedule.enabled: true` 时后台任务真的挂起来；退出 TestClient 后不抛、不悬挂。

    conftest 会把 load_config 的 schedule.enabled 压成 False（hermetic），
    所以这里必须在 create_app 之后、TestClient 进入之前显式打开——
    lifespan 读的是 app.state.config（单一事实源）。
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("adapter: fake\n", encoding="utf-8")
    app = create_app(config_path=cfg_path, db_path=tmp_path / "t.db")
    app.state.config["schedule"] = {
        "enabled": True,
        "run_on_startup": False,
        "sweep_at": ["23:59"],
        "followup_every_minutes": 0,
    }
    with TestClient(app):
        pass  # enter 触发 lifespan 启动（挂调度器），exit 触发关闭（stop + await）


def test_scheduler_disabled_by_default(tmp_path):
    """没写 schedule.enabled 时不挂调度器（默认关，防止"以为没开其实在跑"）。"""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("adapter: fake\n", encoding="utf-8")
    app = create_app(config_path=cfg_path, db_path=tmp_path / "t.db")
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 200


# --------------------------------------------------------------------------- #
# 串行执行
# --------------------------------------------------------------------------- #
def test_scheduler_runs_serially_no_overlap(tmp_path, monkeypatch):
    """主循环串行 await：慢任务期间绝不开始下一轮（上游 20 次/分钟预算不容叠加）。

    断言口径：慢 followup 每次 0.4s、tick 0.05s、窗口 1.2s。
    若不是串行（fire-and-forget），会被调 ~24 次；串行 await 下 ≤ 3 次。
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("adapter: fake\n", encoding="utf-8")
    app = create_app(config_path=cfg_path, db_path=tmp_path / "t.db")
    app.state.config["schedule"] = {
        "enabled": True,
        "run_on_startup": False,
        "sweep_at": [],
        "followup_every_minutes": 1,  # 首次 last_run=None 立即到期，之后每轮 0.4s
    }

    calls: list[str] = []

    def slow_followup(**kwargs):
        import time as t

        t.sleep(0.4)
        calls.append("followup")
        return {"ok": True, "stats": {}}

    monkeypatch.setattr("app.scheduler.dashboard.api_run_followup", slow_followup)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(run_scheduler(app, stop, tick=0.05))
        await asyncio.sleep(1.2)
        stop.set()
        await task

    asyncio.run(scenario())
    assert 1 <= len(calls) <= 3, f"串行约束失效：1.2s 窗口内 followup 被调了 {len(calls)} 次"
