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


def test_timeout_uses_lower_threshold():
    """TIMEOUT 连续 2 轮就该长冷却（2026-11-14 现场）。

    链条：14:47 TIMEOUT（300 秒页面打不开）→ 14:57 TIMEOUT → 15:02 ENVIRONMENT_RISK。
    只差一次就到 3 轮阈值，但风控已经写进锁文件了。
    """
    from app.services.pacer import TIMEOUT_STREAK_LIMIT, streak_limit_for

    assert TIMEOUT_STREAK_LIMIT == 2
    assert streak_limit_for("search 失败（TIMEOUT）：停止本轮，未发送任何招呼") == 2


def test_other_errors_keep_three():
    """别的错误仍是 3 轮 —— 偶发一次 DB 锁不该直接停半小时。"""
    from app.services.pacer import ERROR_STREAK_LIMIT, streak_limit_for

    assert ERROR_STREAK_LIMIT == 3
    assert streak_limit_for("search 失败（NETWORK_ERROR）：readonly database") == 3
    assert streak_limit_for(None) == 3
