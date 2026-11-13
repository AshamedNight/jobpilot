"""爬坡粒度：`pacing.ramp_step_days`（2026-11-12 新增）。

起因：原来固定「每周升一档」，于是"日历进到下一档"和"我们其实一条都没发出去"
可以同时发生。而 50 条/天在 [15,25,40,50] 里是第 4 周才出现，太慢。

现在把粒度做成配置：唯一的旋钮是每几档升一天。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core import pacing


def _cfg(step_days: int, ramp=(15, 20, 25, 30, 40, 50)):
    raw = {
        "segments": [["09:00", "12:00"]],
        "interval_s": [90, 240],
        "round": [3, 5],
        "round_gap_s": [900, 1800],
        "ramp": list(ramp),
        "ramp_start": "2026-11-13",
        "ramp_step_days": step_days,
    }
    return pacing.parse_config(raw, daily_cap=50)


def test_step_two_days_advances_every_other_day():
    cfg = _cfg(2)
    start = date(2026, 11, 13)
    got = [pacing.ramp_target(cfg, start + timedelta(days=d), daily_cap=50) for d in (0, 2, 4, 6, 8, 10)]
    assert got == [15, 20, 25, 30, 40, 50]


def test_reaches_fifty_on_day_ten():
    cfg = _cfg(2)
    assert pacing.ramp_target(cfg, date(2026, 11, 23), daily_cap=50) == 50
    assert pacing.ramp_target(cfg, date(2026, 11, 30), daily_cap=50) == 50, "到顶后保持"


def test_default_is_weekly():
    raw = {"ramp": [15, 25], "ramp_start": "2026-11-13"}
    cfg = pacing.parse_config(raw, daily_cap=50)
    assert cfg.ramp_step_days == 7, "不配就按周，保持老行为"


def test_daily_cap_still_wins():
    cfg = _cfg(1)
    assert pacing.ramp_target(cfg, date(2026, 12, 31), daily_cap=15) == 15, "爬坡目标再高也不许超过额度"


def test_invalid_step_rejected():
    with pytest.raises(pacing.StartupError, match="ramp_step_days"):
        _cfg(0)
