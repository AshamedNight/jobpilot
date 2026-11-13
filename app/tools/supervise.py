"""守护：服务挂了自动拉起，pacer 停了自动起跑（闭环 5）。

## 为什么用"定时检查"而不是"常驻守护进程"

常驻守护自己也会死（用户关窗口、内存不足、蓝屏）。用 Windows 计划任务每 5 分钟跑一次
本脚本，**不需要任何常驻进程**：机器重启、任务被误杀，下一次触发都会自愈。

## 它做什么（顺序固定）

1. `GET /api/status` 探活
2. 不通 → 用 `app.tools.launch` 拉起服务（它会先清掉自家残留实例占的端口）
3. 通了但 `pacing.running=False` → 调 `/api/pacing/start`（幂等，已在跑不会重复启动）
4. 通了且 pacer 在熔断/冷却 → **什么都不做**（熔断是保护，不是故障，不许自动重置）

## 什么情况不重启

- 风控熔断中（`halted_until`）：重启也没用，反而鼓励"故障了就重启"这种坏习惯
- 界面上被人工暂停（`paused=True`）：尊重人工决定

退出码：0 = 无需干预或已自愈；1 = 探活失败且拉起失败（计划任务里能看出失败次数）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = PROJECT_ROOT / "data" / "supervise.log"
DEFAULT_PORT = 8010

#: `next_at` 过期超过这么久还没动作，就认为 pacer 卡死了（2026-11-12 事故）。
#: 现场：15:20 之后 next_at 停在 15:30 再也没 tick，服务却"健康"（/api/status 200），
#: 于是守护一直都在说"无需干预"，实际上一条也没发。
WEDGE_GRACE_SECONDS = 600


def log(message: str) -> None:
    """写一行日志到 `data/supervise.log`（计划任务没有控制台，必须落盘）。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}\n"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    print(line.rstrip())


def fetch_status(port: int, timeout: float = 5.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def is_wedged(status: dict[str, Any], now: datetime | None = None) -> bool:
    """pacer 循环是不是卡死了：计划时间早已过期、又没有正在进行的轮次。

    为什么单看 `running=True` 不够：那个布尔是"启动过"，不是"活着"。
    今天就是 running=True + next_at 过期 20 分钟 + 一条没发。
    """
    pacing = status.get("pacing") or {}
    if not pacing.get("running") or pacing.get("inflight"):
        return False
    next_at = pacing.get("next_at")
    if not next_at:
        return False
    try:
        planned = datetime.fromisoformat(str(next_at))
    except (TypeError, ValueError):
        return False
    if planned.tzinfo is not None:
        planned = planned.replace(tzinfo=None)
    return (now or datetime.now()) - planned > timedelta(seconds=WEDGE_GRACE_SECONDS)


def decide(status: dict[str, Any] | None) -> str:
    """根据探活结果给出动作。纯函数，方便单测。

    返回：
    - `start_service`：服务不通，要拉起
    - `start_pacer`  ：服务通但 pacer 没在跑
    - `do_nothing`   ：健康 / 熔断中 / 人工暂停
    """
    if status is None:
        return "start_service"

    pacing = status.get("pacing") or {}
    if pacing.get("halted_until"):
        return "do_nothing"  # 风控熔断：重启无用，人工处理
    if status.get("paused"):
        return "do_nothing"  # 人工暂停：尊重
    if pacing.get("cooldown_until"):
        return "do_nothing"  # 冷却中：等它自己
    if is_wedged(status):
        return "restart_service"  # 循环卡死：只能重启进程（卡住的线程杀不掉）
    if not pacing.get("running"):
        return "start_pacer"
    return "do_nothing"


def start_service() -> bool:
    """用 launch 拉起服务（detach，不阻塞）。"""
    cmd = [sys.executable, "-m", "app.tools.launch", "--detach"]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=180
        )
        log(f"拉起服务：rc={proc.returncode} {(proc.stdout or '').strip()[:200]}")
        return proc.returncode == 0
    except Exception as exc:  # noqa: BLE001
        log(f"拉起服务失败：{type(exc).__name__} {exc}")
        return False


def start_pacer(port: int) -> bool:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/pacing/start", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        status = data.get("status") or {}
        log(f"起跑 pacer：used={status.get('used')} target={status.get('target')} next={status.get('next_at')}")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"起跑 pacer 失败：{type(exc).__name__} {exc}")
        return False


def run_once(port: int = DEFAULT_PORT) -> int:
    status = fetch_status(port)
    action = decide(status)

    if action == "do_nothing":
        pacing = (status or {}).get("pacing") or {}
        reason = "熔断中" if pacing.get("halted_until") else ("人工暂停" if (status or {}).get("paused") else "健康")
        log(f"检查通过（{reason}），无需干预")
        return 0

    if action in ("start_service", "restart_service"):
        log("服务探活失败 → 拉起" if action == "start_service" else "pacer 循环卡死 → 重启进程")
        if not start_service():
            return 1
        # 起完再探一次，成功就顺手起跑
        for _ in range(20):
            time.sleep(2)
            if fetch_status(port) is not None:
                break
        return 0 if start_pacer(port) else 1

    log("服务在跑但 pacer 未启动 → 起跑")
    return 0 if start_pacer(port) else 1


def main(argv: list[str] | None = None) -> int:
    port = DEFAULT_PORT
    args = list(argv or sys.argv[1:])
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    return run_once(port)


if __name__ == "__main__":
    raise SystemExit(main())
