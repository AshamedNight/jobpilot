"""节奏引擎：同一类错误连续多轮 → 长冷却（2026-11-12 事故）。

现场：14:45–15:20 连续 8 轮 **0 秒失败**（`attempt to write a readonly database`），
引擎每 5 分钟照敲一次平台。这些请求注定失败，却构成"短时间高频请求"的痕迹 —— 15:31 吃到风控。

判据：同一 (verdict + 错误前 40 字) 连续 3 轮 → 冷却拉到 30 分钟；换错误或成功即清零。
"""

from __future__ import annotations

from app.services.pacer import ERROR_STREAK_LIMIT, Pacer


def _pacer() -> Pacer:
    """只测连击计数，不碰数据库/时钟，所以绕过 __init__。"""
    pacer = Pacer.__new__(Pacer)
    pacer.last_error = None
    pacer._err_signature = None
    pacer._err_streak = 0
    return pacer


def test_same_error_repeats_accumulate():
    pacer = _pacer()
    pacer.last_error = "search 失败（NETWORK_ERROR）：readonly database"
    counts = [pacer._track_error_streak("error") for _ in range(3)]
    assert counts == [1, 2, 3]


def test_threshold_is_three():
    assert ERROR_STREAK_LIMIT == 3


def test_different_error_resets_streak():
    pacer = _pacer()
    pacer.last_error = "错误 A"
    pacer._track_error_streak("error")
    pacer._track_error_streak("error")
    pacer.last_error = "错误 B"
    assert pacer._track_error_streak("error") == 1, "换了一种错误，重新计数"


def test_success_resets_streak():
    pacer = _pacer()
    pacer.last_error = "错误 A"
    pacer._track_error_streak("error")
    pacer._track_error_streak("error")
    assert pacer._track_error_streak("ok") == 0, "成功一轮必须清零，否则偶发错误会累积成误判"
    pacer.last_error = "错误 A"
    assert pacer._track_error_streak("error") == 1


def test_risk_and_auth_do_not_use_this_path():
    """风控/登录失效有自己的分支（熔断/停机），不该被这里的连击逻辑吞掉。"""
    pacer = _pacer()
    pacer.last_error = "风控"
    assert pacer._track_error_streak("risk") == 0
    assert pacer._track_error_streak("auth") == 0
