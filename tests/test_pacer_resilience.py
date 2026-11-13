"""闭环 1 事故回归 —— 2026-11-04 真机：一轮 pass 卡了 10 分 24 秒。

现场：用户中途点"停止"，`/api/pacing/status` 显示 `running=false`、
`last_verdict=null`、`halted_until=null`，`data/pacer_state.json` **从没生成** ——
而平台那边其实已经重新上锁（`ENVIRONMENT_RISK`）。风控判定连同 24 小时熔断
被静默吞掉，页面只写"已停止"。

三条用例分别钉住这次事故的三个成因，任何一条退化都会让"看起来正常、其实没跑"重现：

1. 本轮异常必须落成裁决（原来只打日志 → `last_verdict` 永远 null）
2. `stop()` 不许丢掉进行中的 task（丢了会被 GC 回收，本轮结果静默消失）
3. 上一轮还在收尾时 `start()` 必须拒绝（否则两个循环双倍发送）
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import time, timedelta

import pytest

from app import models
from app.core import pacing as P
from app.main import create_app
from app.services.pacer import IDLE_SECONDS


@pytest.fixture()
def app(tmp_path):
    db = tmp_path / "pacer-resilience.db"
    models.init_db(db).close()
    application = create_app(db_path=db)
    application.state.paused = False
    application.state.risk_locked = False
    return application


def _open_all_day(pacer) -> None:
    """把时段改成全天，让 `_tick()` 走到"发送"那一步（默认配置此刻可能不在时段内）。"""
    pacer._pcfg = replace(pacer._pcfg, segments=(P.Segment(time(0, 0), time(23, 59)),))


# --------------------------------------------------------------------------- #
# 1. 异常也要留裁决
# --------------------------------------------------------------------------- #
def test_tick_exception_records_error_verdict(app, monkeypatch):
    """一轮抛异常时，`last_verdict` 必须是 `error`，且带冷却。

    事故现场的 `last_verdict=null` 让人无法区分两种情况：
    "引擎没跑"和"跑了但卡死"。两者处置完全不同，页面必须能分。
    """
    pacer = app.state.pacer
    _open_all_day(pacer)

    def boom() -> None:
        raise RuntimeError("上游卡死")

    monkeypatch.setattr(pacer, "_pass_once", boom)

    delay = asyncio.run(pacer._tick())

    assert pacer.last_verdict == "error", "异常必须落成裁决，否则页面永远显示 null"
    assert pacer.last_verdict_at is not None, "裁决必须带时间戳，否则旧结论会被当成新结论"
    assert "RuntimeError" in (pacer.last_error or "")
    assert pacer.cooldown_until is not None, "异常必须退避，否则下一秒又撞一次"
    assert delay == 5 * 60, f"error 档冷却应为 5 分钟，实际 {delay}"


def test_tick_clears_inflight_after_exception(app, monkeypatch):
    """异常路径也必须复位 `inflight`，否则页面会永远显示"正在发"。"""
    pacer = app.state.pacer
    _open_all_day(pacer)

    def boom() -> None:
        raise RuntimeError("上游卡死")

    monkeypatch.setattr(pacer, "_pass_once", boom)
    asyncio.run(pacer._tick())

    assert pacer.status()["inflight"] is False


# --------------------------------------------------------------------------- #
# 2. stop() 不许丢 task 引用
# --------------------------------------------------------------------------- #
def test_stop_keeps_pending_task_reference(app):
    """停止时进行中的那一轮不能被丢掉：它的结果（含风控熔断）必须还能落盘。

    原实现 `stop()` 里 `self._task = None`，asyncio 对 task 只保留弱引用，
    挂起中的 task 失去最后一个强引用就会被 GC —— 结果永远没人消费。
    """
    pacer = app.state.pacer
    _open_all_day(pacer)

    async def scenario() -> None:
        gate = asyncio.Event()

        async def slow_tick() -> int:
            await gate.wait()
            return IDLE_SECONDS

        pacer._tick = slow_tick  # type: ignore[method-assign]
        assert pacer.start()["ok"] is True
        await asyncio.sleep(0.02)

        pacer.stop()

        task = pacer._task
        assert task is not None and not task.done(), (
            "stop() 不许丢掉进行中的 task 引用：丢了会被 GC 回收，本轮结果静默丢失"
        )

        gate.set()
        await asyncio.wait_for(task, timeout=2)
        await asyncio.sleep(0)  # 让 done 回调跑完
        assert pacer._task is None, "循环退出后引用要清掉，否则永远 start 不起来"
        assert pacer.running is False

    asyncio.run(scenario())


def test_start_refuses_while_previous_pass_is_finishing(app):
    """`stop()` 之后上一轮还在收尾时，再 `start()` 必须拒绝（否则双循环双倍发送）。"""
    pacer = app.state.pacer
    _open_all_day(pacer)

    async def scenario() -> None:
        gate = asyncio.Event()

        async def slow_tick() -> int:
            await gate.wait()
            return IDLE_SECONDS

        pacer._tick = slow_tick  # type: ignore[method-assign]
        pacer.start()
        await asyncio.sleep(0.02)
        pacer.stop()

        again = pacer.start()
        assert again["ok"] is False
        assert "收尾" in again["error"]

        gate.set()
        await asyncio.wait_for(pacer._task, timeout=2)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 3. 状态里的可观测字段
# --------------------------------------------------------------------------- #
def test_status_exposes_inflight(app):
    """`inflight` 是"这一拍正在发"的唯一可观测信号（`last_verdict` 轮内必然为 null）。"""
    status = app.state.pacer.status()
    assert "inflight" in status
    assert status["inflight"] is False
    assert "pass_started_at" in status


def test_cap_only_tightens_today_target(app):
    """小批量试跑：`cap` 只收紧日目标，且跨天失效。"""
    pacer = app.state.pacer
    pacer._pcfg = replace(pacer._pcfg, ramp=(15,))
    pacer.cfg["quota"]["daily_greet"] = 15

    assert pacer._target_today() == 15

    pacer._cap_today = 3
    pacer._cap_day = pacer._today_utc()
    assert pacer._target_today() == 3, "cap=3 时今日目标必须是 3"

    pacer._cap_day = pacer._today_utc() - timedelta(days=1)
    assert pacer._target_today() == 15, "cap 是当天的，跨天必须失效"
