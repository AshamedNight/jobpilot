"""节奏引擎的三个入口（闭环 1）：看状态、开、关。

刻意**不放在 `/api/run/*` 下**：那个前缀被 `main.py` 的中间件接管，
语义是"这个请求本身就是一次业务发送"。而 `start` 只是"把后台推进器打开"，
真正的发送发生在之后每一拍里，由 pacer 自己申请业务互斥。
放在 `/api/run/` 下会导致"启动"这一步被连接任务挡住，却挡不住后续发送——
比挡不住更糟，因为它给人"已经受控"的错觉。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(tags=["pacing"])


def _pacer(request: Request) -> Any:
    return getattr(request.app.state, "pacer", None)


@router.get("/api/pacing/status")
def api_pacing_status(request: Request) -> dict[str, Any]:
    """今日进度 / 当前时段 / 下一次什么时候发 / 是否在冷却。

    **只读内存 + 一次本地 DB count**，不发任何上游请求，可以放心轮询。
    """
    pacer = _pacer(request)
    if pacer is None:
        return {
            "enabled": False,
            "running": False,
            "config_error": "节奏引擎未装配（app.state.pacer 不存在）",
        }
    return pacer.status()


@router.post("/api/pacing/start")
async def api_pacing_start(request: Request, cap: int | None = None) -> dict[str, Any]:
    """启动后台推进器。已在运行 → 幂等返回。

    `cap`：今日发送硬上限（小批量试跑）。例如 `?cap=3` = 今天最多 3 条，
    跑通再放开。只收紧、不放大，跨天自动失效。
    """
    pacer = _pacer(request)
    if pacer is None:
        return {"ok": False, "error": "节奏引擎未装配"}

    # 连接任务进行中不许开节奏：否则"连接"和"发送"会抢同一个浏览器/凭据
    manager = getattr(request.app.state, "connect", None)
    if manager is not None:
        blocked = manager.acquire_business()
        if blocked:
            return {"ok": False, "error": blocked}
        manager.release_business()

    return pacer.start(cap=cap)


@router.post("/api/pacing/stop")
def api_pacing_stop(request: Request) -> dict[str, Any]:
    """停止后台推进器。已停止也返回 ok（幂等）。"""
    pacer = _pacer(request)
    if pacer is None:
        return {"ok": False, "error": "节奏引擎未装配"}
    return pacer.stop()
