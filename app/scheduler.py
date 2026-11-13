"""定时调度器：启动后按 `config.yaml` 的 `schedule` 段自动跑 sweep / followup。

为什么需要它：`schedule.sweep_at` / `followup_every_minutes` 此前没有任何代码消费，
"自动"只能靠人手动 POST `/api/run/sweep`。本模块在 lifespan 里挂一个 asyncio 后台任务，
到点直接调 `app.api.dashboard` 的编排函数（**业务逻辑零复制**，规则仍全在 core/ 与 dashboard）。

三条硬约束：

1. **编排函数是同步、阻塞的**（真机一轮 1~2 分钟）→ 一律 `asyncio.to_thread` 跑，
   不许阻塞事件循环，否则 `/api/status` 轮询会被卡死。
2. **不许并发重入**：主循环**串行 await** 每一轮（上一轮没跑完，循环就停在那里，
   下一轮自然不会开始）。上游请求预算是 20 次/分钟且跨进程共享，
   两轮叠加必撞 `REQUEST_BUDGET_EXHAUSTED`。代价是关机时会等当前轮跑完
   （最多一轮 sweep 的时长）——中断已发出的招呼比晚退几秒更糟。
3. **不自己实现闸门**：paused / risk_locked / 额度截断全由 `api_run_sweep` /
   `api_run_followup` 内部的既有守卫处理，调度器只负责"到点叫人"。
"""

from __future__ import annotations

import asyncio
import logging
import time as time_mod
from datetime import datetime, time, timedelta
from typing import Any, Callable

from app import models
from app.api import dashboard

log = logging.getLogger("jobpilot.scheduler")

#: 调度循环的唤醒间隔（秒）。30s 的误差对"09:30 打招呼"这种场景足够，
#: 再小只是空转；sweep 时间点本身允许分钟级漂移（上游节流下跑一轮就要 1~2 分钟）。
TICK_SECONDS = 30


class _ShimRequest:
    """最小 Request 替身：编排函数只用 `request.app.state.*`，不必真造 HTTP 请求。"""

    def __init__(self, app: Any) -> None:
        self.app = app


# --------------------------------------------------------------------------- #
# 纯函数（可测，无 IO）
# --------------------------------------------------------------------------- #
def parse_sweep_times(raw: Any) -> list[time]:
    """`["09:30", "20:30"]` → `[time(9,30), time(20,30)]`。非法项丢弃，不抛。"""
    times: list[time] = []
    for item in raw or []:
        try:
            times.append(datetime.strptime(str(item).strip(), "%H:%M").time())
        except (ValueError, TypeError):
            log.warning("schedule.sweep_at 里有非法时间点，已跳过：%r", item)
    return sorted(times)


def next_sweep(now: datetime, times: list[time]) -> datetime | None:
    """距离 `now` 最近的一次 sweep 时刻（今天不行就明天）。空表 → None。"""
    if not times:
        return None
    today = now.date()
    for t in times:
        candidate = datetime.combine(today, t)
        if candidate > now:
            return candidate
    return datetime.combine(today + timedelta(days=1), times[0])


def followup_due(last_run: float | None, every_minutes: int, now_ts: float) -> bool:
    """距上次 followup 是否已满 `every_minutes`。从未跑过 → 到期。"""
    if every_minutes <= 0:
        return False
    if last_run is None:
        return True
    return (now_ts - last_run) >= every_minutes * 60


# --------------------------------------------------------------------------- #
# 后台任务
# --------------------------------------------------------------------------- #
async def run_scheduler(app: Any, stop: asyncio.Event, tick: int = TICK_SECONDS) -> None:
    """调度主循环。挂进 lifespan，stop.set() 后退出（服务关闭时）。"""
    cfg: dict[str, Any] = app.state.config
    schedule = cfg.get("schedule") or {}
    times = parse_sweep_times(schedule.get("sweep_at"))
    every = int(schedule.get("followup_every_minutes") or 0)
    run_on_startup = bool(schedule.get("run_on_startup"))

    sweep_at = next_sweep(datetime.now(), times)
    last_followup: float | None = None

    async def _run(name: str, fn: Callable[..., dict[str, Any]]) -> None:
        """串行执行一轮编排（约束 2）：调用方 await 它，循环自然不重叠。"""
        try:
            result = await asyncio.to_thread(_invoke, app, fn)
            if not result.get("ok"):
                log.warning("调度器：%s 未执行：%s", name, result.get("error"))
            else:
                log.info("调度器：%s 完成 stats=%s", name, result.get("stats"))
        except Exception:  # 调度器自己绝不许把 lifespan 搞崩
            log.exception("调度器：%s 抛异常", name)

    if run_on_startup and sweep_at is not None:
        await _run("sweep(startup)", dashboard.api_run_sweep)

    while not stop.is_set():
        now = datetime.now()
        if sweep_at is not None and now >= sweep_at:
            await _run("sweep", dashboard.api_run_sweep)
            sweep_at = next_sweep(datetime.now(), times)
        if followup_due(last_followup, every, time_mod.time()):
            await _run("followup", dashboard.api_run_followup)
            last_followup = time_mod.time()
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick)
        except asyncio.TimeoutError:
            pass


def _invoke(app: Any, fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """在线程里调编排函数：自带 db 连接与 shim request（约束 1 的执行体）。"""
    conn = models.connect(app.state.db_path)
    try:
        return fn(request=_ShimRequest(app), db=conn, cfg=app.state.config)
    finally:
        conn.close()
