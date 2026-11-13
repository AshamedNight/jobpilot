"""闭环 5：守护决策（纯函数）。

这里只测"看到什么状态该做什么"，不真的起进程 —— 起进程的部分由人工在生产上验。
决策错一次的代价很大：把风控熔断当成故障去重启，等于帮平台把自己的脸贴上去。
"""

from __future__ import annotations

from app.tools.supervise import decide


def test_service_down_should_start():
    assert decide(None) == "start_service"


def test_healthy_should_do_nothing():
    status = {"paused": False, "pacing": {"running": True}}
    assert decide(status) == "do_nothing"


def test_service_up_but_pacer_stopped_should_start_pacer():
    status = {"paused": False, "pacing": {"running": False}}
    assert decide(status) == "start_pacer"


def test_risk_halt_must_not_be_auto_restarted():
    """熔断是保护，不是故障。自动重启会让"故障了就重启"变成习惯。"""
    status = {"paused": False, "pacing": {"running": False, "halted_until": "2026-11-13T12:35:52"}}
    assert decide(status) == "do_nothing"


def test_cooldown_must_not_be_auto_restarted():
    status = {"paused": False, "pacing": {"running": True, "cooldown_until": "2026-11-12T13:00:00"}}
    assert decide(status) == "do_nothing"


def test_manual_pause_is_respected():
    status = {"paused": True, "pacing": {"running": False}}
    assert decide(status) == "do_nothing"


def test_missing_pacing_block_falls_back_to_start_pacer():
    """老接口/异常响应没有 pacing 字段时，按"没在跑"处理（保守但可自愈）。"""
    assert decide({"paused": False}) == "start_pacer"


def test_wedged_pacer_should_restart():
    """2026-11-12：next_at 过期 20 分钟、inflight=False、running=True → 循环卡死。"""
    from datetime import datetime, timedelta

    from app.tools.supervise import is_wedged

    now = datetime.now()  # 用真实时钟：decide() 内部也读真实时钟，写死会把测试变成"看运气"
    status = {
        "paused": False,
        "pacing": {
            "running": True,
            "inflight": False,
            "next_at": (now - timedelta(minutes=20)).isoformat(timespec="seconds"),
        },
    }
    assert is_wedged(status, now) is True
    assert decide(status) == "restart_service"


def test_not_wedged_when_about_to_act():
    from datetime import datetime, timedelta

    from app.tools.supervise import is_wedged

    now = datetime.now()  # 用真实时钟：decide() 内部也读真实时钟，写死会把测试变成"看运气"
    status = {
        "paused": False,
        "pacing": {
            "running": True,
            "inflight": False,
            "next_at": (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
        },
    }
    assert is_wedged(status, now) is False


def test_inflight_is_not_wedged():
    """正在跑的一轮可能很久（上游单页 60–180 秒，甚至更久），不许当卡死重启。"""
    from datetime import datetime, timedelta

    from app.tools.supervise import is_wedged

    now = datetime.now()  # 用真实时钟：decide() 内部也读真实时钟，写死会把测试变成"看运气"
    status = {
        "pacing": {
            "running": True,
            "inflight": True,
            "next_at": (now - timedelta(minutes=30)).isoformat(timespec="seconds"),
        }
    }
    assert is_wedged(status, now) is False
