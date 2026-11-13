"""节奏验收报告（闭环 1 的验收口径，可直接跑）。

验收标准（`现状盘点与推进路线-v1.md` 闭环 1）：

- 每日 ≥ 目标条数（真机按 `pacing.ramp` 当前档位）
- **无任何 RISK 类错误码**
- DB 里相邻 `greet` 的时间差 **≥ 90 秒**

前两条在 `runs` 表里看，第三条只能从 `actions.created_at` 算 —— 这个脚本就是把第三条
变成一条命令，省得每次手工查 SQL 还数错。

用法：

    python -m app.tools.pacing_report                 # 默认看最近 7 天
    python -m app.tools.pacing_report --days 3
    python -m app.tools.pacing_report --db data/jobpilot-new.db

只有读操作，不写库、不联网。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 间隔硬下限（与 `app/core/pacing.py::INTERVAL_FLOOR` 同口径）
INTERVAL_FLOOR = 60

#: 验收用的目标线：闭环 1 要求"相邻 greet ≥ 90 秒"，这是产品口径不是配置值
ACCEPT_INTERVAL = 90

RISK_CODES = ("ACCOUNT_RISK", "ENVIRONMENT_RISK", "ENVIRONMENT_RISK_LOCKED", "LOGIN_RISK_CONTROL")


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def collect(db_path: str | Path, days: int = 7) -> dict[str, Any]:
    """读出最近 `days` 天的 greet 记录，算间隔与模板分布。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, target, status, response_json, created_at FROM actions"
            " WHERE action_type = 'greet' ORDER BY id"
        ).fetchall()
        runs = conn.execute(
            "SELECT kind, started_at, stats_json, error FROM runs ORDER BY run_id DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()

    entries: list[dict[str, Any]] = []
    for row in rows:
        moment = _parse_ts(row["created_at"])
        if moment is None:
            continue
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(row["response_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        entries.append(
            {
                "id": row["id"],
                "target": row["target"],
                "status": row["status"],
                "at": moment,
                "tpl": payload.get("tpl"),
                "already": bool(payload.get("already_greeted")),
            }
        )

    #: 窗口起点（epoch 秒）。`None` = 不限制（days <= 0）。
    #: 锚点用"最新一条 greet"，而不是 `now()`：验收看的是**数据窗口**，
    #: 隔几天再回头跑同一份库，结论不该变。
    cutoff_ts: float | None = None
    if days > 0 and entries:
        newest = max(e["at"] for e in entries)
        cutoff_ts = newest.timestamp() - days * 86400
        entries = [e for e in entries if e["at"].timestamp() >= cutoff_ts]

    # 相邻间隔：只看真正新发的（already_greeted 不算一次新动作）
    # 新发条数 / 间隔 / 模板：只看**真的发出去**的。
    # `already_greeted` 不算新动作；`failed` 更不能算 —— 2026-11-08 的两条 AUTH_EXPIRED
    # 曾把"新发"刷高 2 条，直接让验收数失真。
    fresh = [e for e in entries if not e["already"] and e["status"] == "ok"]
    failed_count = sum(1 for e in entries if e["status"] != "ok")
    gaps: list[float] = []
    for prev, nxt in zip(fresh, fresh[1:]):
        gaps.append((nxt["at"] - prev["at"]).total_seconds())

    by_day: dict[str, int] = {}
    for item in fresh:
        key = item["at"].astimezone().strftime("%Y-%m-%d")
        by_day[key] = by_day.get(key, 0) + 1

    tpl_counts: dict[str, int] = {}
    for item in fresh:
        key = str(item.get("tpl") or "—")
        tpl_counts[key] = tpl_counts.get(key, 0) + 1

    # 【2026-11-05 修】风控命中必须**落在同一个窗口里**。
    # 原来这段没过滤：`--days 1` 也会把几天前那次 ENVIRONMENT_RISK 列出来，
    # 三天验收时看到"风控类失败 3 条"会误判成今天又撞了。
    risk_hits = []
    for row in runs:
        if not row["error"]:
            continue
        if not any(code in str(row["error"]) for code in RISK_CODES):
            continue
        moment = _parse_ts(row["started_at"])
        if cutoff_ts is not None and (moment is None or moment.timestamp() < cutoff_ts):
            continue
        risk_hits.append({"at": str(row["started_at"]), "error": row["error"]})

    return {
        "db": str(db_path),
        "total_greet": len(entries),
        "fresh": len(fresh),
        "failed": failed_count,
        "by_day": dict(sorted(by_day.items())),
        "gaps": gaps,
        "tpl_counts": tpl_counts,
        "risk_hits": risk_hits,
    }


def _fmt_seconds(value: float) -> str:
    if value >= 3600:
        return f"{value / 3600:.1f}h"
    if value >= 60:
        return f"{value / 60:.1f}m"
    return f"{value:.0f}s"


def render(report: dict[str, Any]) -> str:
    lines: list[str] = [f"库：{report['db']}", ""]

    lines.append("每日新发条数：")
    if report["by_day"]:
        for day, count in report["by_day"].items():
            lines.append(f"  {day}  {count:>4} 条")
    else:
        lines.append("  （没有记录）")
    lines.append(f"  合计（去重后新发）：{report['fresh']} 条")
    if report.get("failed"):
        # 失败不占额度、也不算新发，但要在报告里可见 —— 否则"怎么只有 13 条"会查半天
        lines.append(f"  另有失败/未确认：{report['failed']} 条（不计入新发）")

    lines.append("")
    lines.append(f"相邻 greet 间隔（验收线 ≥ {ACCEPT_INTERVAL}s）：")
    gaps = report["gaps"]
    if gaps:
        too_close = [g for g in gaps if g < ACCEPT_INTERVAL]
        lines.append(f"  样本 {len(gaps)} 个")
        lines.append(f"  最小 {_fmt_seconds(min(gaps))} / 中位 {_fmt_seconds(statistics.median(gaps))}"
                     f" / 最大 {_fmt_seconds(max(gaps))}")
        verdict = "通过" if not too_close else f"不通过（{len(too_close)} 个间隔过近）"
        lines.append(f"  结论：{verdict}")
        if too_close:
            lines.append(f"  过近样本：{[round(g) for g in too_close[:10]]} 秒")
    else:
        lines.append("  样本不足（至少需要 2 条新发）")

    lines.append("")
    lines.append("文案模板使用次数：")
    if report["tpl_counts"]:
        for tpl, count in sorted(report["tpl_counts"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {tpl:<8} {count:>4}")
        lines.append(f"  共 {len(report['tpl_counts'])} 个模板被用到")
    else:
        lines.append("  （没有记录）")

    lines.append("")
    lines.append("风控类失败（验收要求：0 条）：")
    if report["risk_hits"]:
        for hit in report["risk_hits"][:10]:
            lines.append(f"  {hit['at']}  {hit['error']}")
    else:
        lines.append("  无")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="节奏引擎验收报告（只读）")
    parser.add_argument("--db", default="data/jobpilot-new.db", help="sqlite 库路径")
    parser.add_argument("--days", type=int, default=7, help="看最近几天，0 = 全部")
    args = parser.parse_args(argv)

    path = Path(args.db)
    if not path.exists():
        print(f"找不到库：{path}")
        return 2

    print(render(collect(path, args.days)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
