"""每日探测配额：把"手贱"变成会失败的硬限制（2026-11-13）。

## 为什么要这个

过去 9 天我们撞了 5 次环境风控，其中**至少 3 次是诊断性请求自己敲出来的**：

- 解风控后 40 分钟内做了 5 次搜索（手动对比关键词、实测城市码）
- 同一进程连做 3 次 `search`，第 3 次必被判环境异常
- 探测刚失败就再试一次，把"刚被拒"变成"反复被拒"

人的自控力靠不住，所以做成配额：**每天最多 N 次探测类请求，超了直接拒绝执行**。
要突破必须显式 `--force`，并且会记进日志 —— 让"我知道我在冒险"变成一次有意识的动作。

## 记的是什么

按**本地日期**计数（不是 UTC）：人的直觉是天亮算新的一天。
计数文件：`data/probe_budget.json`

```json
{"day": "2026-11-13", "count": 2, "events": [{"at": "...", "kind": "recover_probe"}]}
```
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUDGET_PATH = PROJECT_ROOT / "data" / "probe_budget.json"

#: 允许用环境变量把配额文件指到别处。
#: 为什么必须可注入：`recover` 在测试里也会被调用，如果配额写在真实 `data/` 下，
#: 跑几次测试就把"今天还能探测几次"用光了（而且测试之间互相影响）。
#: 生产用默认路径，测试由 conftest 指到临时文件。
ENV_OVERRIDE = "JOBPILOT_PROBE_BUDGET"

#: 默认每日上限。3 = 一次恢复探测 + 一次招呼探针 + 一次意外。
DEFAULT_LIMIT = 3


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _path() -> Path:
    override = os.environ.get(ENV_OVERRIDE)
    return Path(override) if override else BUDGET_PATH


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if data.get("day") != _today():
        # 跨天重置：只留计数，历史不跨天累计
        data = {"day": _today(), "count": 0, "events": []}
    data.setdefault("count", 0)
    data.setdefault("events", [])
    return data


def _save(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def remaining(limit: int = DEFAULT_LIMIT) -> int:
    return max(int(limit) - int(_load().get("count") or 0), 0)


def status(limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    data = _load()
    return {
        "day": data["day"],
        "count": int(data.get("count") or 0),
        "limit": int(limit),
        "remaining": remaining(limit),
        "events": data.get("events") or [],
    }


def consume(kind: str, *, limit: int = DEFAULT_LIMIT) -> tuple[bool, dict[str, Any]]:
    """消耗一次配额。返回 `(是否放行, 当前状态)`。

    放行时才 +1；被拒时**不改计数**（拒绝本身不产生请求）。
    """
    data = _load()
    used = int(data.get("count") or 0)
    if used >= int(limit):
        return False, {
            "day": data["day"],
            "count": used,
            "limit": int(limit),
            "remaining": 0,
            "events": data.get("events") or [],
        }

    data["count"] = used + 1
    events = list(data.get("events") or [])
    events.append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind})
    data["events"] = events[-20:]
    _save(data)
    return True, {
        "day": data["day"],
        "count": data["count"],
        "limit": int(limit),
        "remaining": max(int(limit) - data["count"], 0),
        "events": events,
    }


def describe(state: dict[str, Any]) -> str:
    return f"今日探测配额 {state['count']}/{state['limit']}（剩 {state['remaining']}）"
