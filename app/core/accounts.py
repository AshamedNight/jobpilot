"""闭环 3：多账号。账号 = 一套完全隔离的「库 + 数据目录 + Chrome profile + 调试端口」。

## 为什么"换账号"不等于"换个登录"

本项目是 `browser_source=existing-browser`：CLI **不读** `auth/session.enc`，
身份完全由那个 Chrome profile 的实时登录态决定（`boss_agent_cli/api/browser_source.py`
的 POLICIES 里 `use_stored_credentials=False`）。所以：

- 同一个 Chrome profile 开新标签 = 同一个账号，**换不了号**
- 换 `data_dir` 只换"CLI 的工作目录/预算账本"，**不换身份**
- 要换号，必须换 **Chrome profile**（不同 `--user-data-dir`）+ 另一个调试端口
  （同一 profile 不能同时开两个实例）

推论：账号切换是**停机切换**，不是并行跑两条线。同时跑两条线只会让风控翻倍，
换来的收益不如把一条线跑稳。

## 隔离什么

| 资源 | 为什么必须分开 |
|---|---|
| `db`（我们的 sqlite） | 额度、claim、会话混在一起就再也分不开谁打过谁 |
| `data_dir`（上游工作目录） | 里面有 `cache/boss_agent.db` 的 `greet_records` 去重账、`cdp_risk_lock.json` 风险锁 |
| `chrome_profile` + `cdp_port` | 身份本身 |
| `pacer_state` | 熔断/冷却状态不该跨账号传播 |

## 切换方式

```bash
python -m app.tools.accounts list          # 看有哪些、当前是哪个
python -m app.tools.accounts switch a      # 切到 A 号（只写指针，不重启）
python -m app.tools.launch --detach --cap 15   # 启动时按指针装环境
```

指针文件：`data/active_account.json`。**启动器与守护都会读它**，
所以切完只要重启服务（或等服务被守护拉起）就生效。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POINTER_PATH = PROJECT_ROOT / "data" / "active_account.json"

#: 每个账号必须提供的字段（缺一个就拒绝启用，不允许"半套配置"）
REQUIRED_FIELDS = ("id", "name", "db", "data_dir", "chrome_profile", "cdp_port")

#: 多账号**并行**时每个账号要占一个独立的**服务端口**（自己的控制台、自己的 pacer）。
#: 不配就退回 8010（单账号场景）；配了就必须互不相同。
DEFAULT_SERVICE_PORT = 8010


class AccountError(ValueError):
    """账号配置不合法（缺字段 / 重名 / 端口冲突）。"""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def registry(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """从配置里取账号清单，并做完整性校验。

    校验是**启动期**做的：宁可服务起不来，也不要带着半个账号配置去发招呼
    （发错号比发不出去严重得多）。
    """
    items = cfg.get("accounts") or []
    if not isinstance(items, list) or not items:
        return []

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_ports: dict[int, str] = {}
    seen_service: dict[int, str] = {}
    for raw in items:
        if not isinstance(raw, dict):
            raise AccountError(f"accounts 条目必须是对象：{raw!r}")
        missing = [field for field in REQUIRED_FIELDS if not _clean(raw.get(field))]
        if missing:
            raise AccountError(f"账号 {raw.get('id') or '?'} 缺少字段：{'、'.join(missing)}")
        account = {key: raw.get(key) for key in raw}
        account["id"] = _clean(raw.get("id"))
        account["cdp_port"] = int(raw.get("cdp_port"))
        account["service_port"] = int(raw.get("service_port") or DEFAULT_SERVICE_PORT)
        account["enabled"] = bool(raw.get("enabled", True))
        if account["id"] in seen_ids:
            raise AccountError(f"账号 id 重复：{account['id']}")
        if account["cdp_port"] in seen_ports:
            raise AccountError(
                f"账号 {account['id']} 与 {seen_ports[account['cdp_port']]} 用了同一个调试端口"
                f" {account['cdp_port']}（同一 profile 不能开两个实例，端口必须分开）"
            )
        if account["service_port"] in seen_service:
            raise AccountError(
                f"账号 {account['id']} 与 {seen_service[account['service_port']]} 用了同一个服务端口"
                f" {account['service_port']}（并行时每个账号要有自己的控制台端口）"
            )
        seen_ids.add(account["id"])
        seen_ports[account["cdp_port"]] = account["id"]
        seen_service[account["service_port"]] = account["id"]
        out.append(account)
    return out


def find(cfg: dict[str, Any], account_id: str) -> dict[str, Any] | None:
    for account in registry(cfg):
        if account["id"] == account_id:
            return account
    return None


def read_pointer() -> str | None:
    """读当前账号 id（不存在或损坏都返回 None，不抛）。"""
    try:
        data = json.loads(POINTER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    active = _clean(data.get("active"))
    return active or None


def write_pointer(account_id: str) -> None:
    POINTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    POINTER_PATH.write_text(
        json.dumps({"active": account_id}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def active_account(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """当前生效的账号：**环境变量优先**，其次指针文件。

    为什么环境变量优先：`launch` 启动子进程时会把决定好的账号写进环境，
    这样即使指针文件在中途被改，**已启动的进程**仍然稳定地跑在自己那个账号上。
    """
    wanted = _clean(os.environ.get("JOBPILOT_ACCOUNT")) or read_pointer()
    if not wanted:
        return None
    return find(cfg, wanted)


def apply_override(cfg: dict[str, Any]) -> dict[str, Any]:
    """按当前账号覆盖 `adapter_options.data_dir`（在 `load_config` 末尾调用）。

    只覆盖"数据目录"这一类资源路径，**不覆盖额度/节奏/目标**：
    那些是业务策略，应该由人决定，不该跟着账号漂移。

    **只认环境变量 `JOBPILOT_ACCOUNT`，不读指针文件。** 原因（2026-11-12 踩过）：
    一旦 `load_config()` 也读指针，那么"切过号"之后所有**没经过启动器**的入口
    （测试、临时脚本、直接把 uvicorn 拉起来的场景）都会静默换掉 data_dir ——
    测试当场变红（风险锁写到了另一个目录）。配置加载必须由**调用方显式声明**是谁。
    """
    wanted = _clean(os.environ.get("JOBPILOT_ACCOUNT"))
    if not wanted:
        return cfg
    account = find(cfg, wanted)
    if not account:
        return cfg

    adapter_options = dict(cfg.get("adapter_options") or {})
    adapter_options["data_dir"] = account["data_dir"]
    cfg["adapter_options"] = adapter_options

    # 【2026-11-12】每个账号可以有**自己的节奏与额度**（可选，配了才覆盖）。
    #
    # 为什么必须能分开：多个号并行时，如果共用一套 `segments`，它们会在同一时间段里
    # 同时敲门 —— 那是把"同 IP 多号"的风险叠满。错峰是唯一能降风险的手段，
    # 而错峰的前提就是每个号有自己的时段/间隔。
    #
    # 为什么是浅合并：`segments` 是个列表，账号想整段替���就必须整体覆盖；
    # 而 `interval_s` / `round` 这类想只改一项的，浅合并也能顺着全局值走。
    for section in ("pacing", "quota"):
        override = account.get(section)
        if isinstance(override, dict) and override:
            merged = dict(cfg.get(section) or {})
            merged.update(override)
            cfg[section] = merged
    cfg["active_account"] = {
        "id": account["id"],
        "name": account["name"],
        "db": account["db"],
        "cdp_port": account["cdp_port"],
    }
    return cfg


def env_for(account: dict[str, Any]) -> dict[str, str]:
    """把这个账号翻译成子进程需要的环境变量。

    `JOBPILOT_DB` 决定我们的库，`JOBPILOT_BOSS_*` 决定上游目录，
    `JOBPILOT_ACCOUNT` 让 `load_config` 认得"现在是谁"。
    """
    env = {
        "JOBPILOT_ACCOUNT": account["id"],
        "JOBPILOT_DB": str(PROJECT_ROOT / account["db"])
        if not Path(account["db"]).is_absolute()
        else account["db"],
        "JOBPILOT_BOSS_CRED_DIR": account["data_dir"],
    }
    budget = _clean(account.get("budget_dir"))
    if budget:
        env["JOBPILOT_BOSS_BUDGET_DIR"] = budget
    profile = _clean(account.get("chrome_profile"))
    if profile:
        env["JOBPILOT_CHROME_PROFILE_DIR"] = profile
    exe = _clean(account.get("chrome_exe"))
    if exe:
        env["JOBPILOT_CHROME_EXE"] = exe
    return env


def describe(account: dict[str, Any]) -> str:
    """给 CLI/日志用的一行摘要。"""
    exists = "✓" if Path(account["chrome_profile"]).exists() else "✗"
    return (
        f"{account['id']:<4} {account['name'][:18]:<20} "
        f"port={account['cdp_port']} profile{exists} db={Path(account['db']).name}"
    )
