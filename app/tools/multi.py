"""多账号并行编排（闭环 3）。

## 目标模型（用户 2026-11-12 明确）

- **一个使用者 → 多个账号 → 每个账号各自 50+ 条/天**，额度互不共享
- 不是"一台主机供多人共享一份额度"

## 实现方式：一个账号 = 一个独立服务进程

```
账号 b → 服务 :8010 → Chrome :9222 → profile chrome-boss → db jobpilot-new.db
账号 a → 服务 :8020 → Chrome :9223 → profile chrome-boss-a → db jobpilot-a.db
```

每个进程用 `JOBPILOT_ACCOUNT=<id>` 装环境，于是：

| 资源 | 隔离方式 |
|---|---|
| 数据库（额度/claim/会话） | `accounts[].db` |
| 上游工作目录（去重账本、风险锁） | `accounts[].data_dir` |
| Chrome 身份 | `accounts[].chrome_profile` + `cdp_port` |
| 熔断/冷却状态 | `pacer_state_<账号id>.json`（`main.py` 里按账号推导） |
| 日目标 / 爬坡 | 各自进程各算各的（`pacing` 配置 + `quota` 是进程级）|

## 用法

```bash
python -m app.tools.multi status      # 汇总所有账号的实时状态（只读，不碰平台）
python -m app.tools.multi start       # 起所有 enabled 账号（每个账号一个进程）
python -m app.tools.multi stop        # 停（只杀本项目自己起的 python）
```

## 一句必须说清的风险

**同机、同 IP、同网络下多个账号同时高频发招呼，平台侧的关联风险会显著上升。**
技术上能并行，不代表应该无脑并行。建议：

1. 第一个号先跑稳（3–5 天零风控），再加第二个
2. 两个号的时段错开（例如 A 号 09:00–12:00 主发，B 号 14:00–18:00 主发）
3. 有条件就让不同账号走不同出口 IP
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

from app.config import load_config
from app.core import accounts as accounts_mod

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _get(port: int, path: str, timeout: float = 5.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _listener_pid(port: int) -> int | None:
    """用 netstat 找监听者（psutil 在本机对 8010 有时看不到，见 launch.py 的注释）。"""
    import subprocess

    try:
        out = subprocess.run(  # noqa: S603, S607
            ["netstat", "-ano"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10
        ).stdout or ""
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
                return int(parts[4])
    except Exception:
        return None
    return None



def _risk_locked(account: dict) -> bool:
    """看**锁文件**，不要问 HTTP。

    2026-11-13 踩过：先查 `/api/status` 判断锁，但那个服务是恢复**之前**启动的，
    内存里留着 fail-closed 标记 —— 于是锁明明已经清掉，脚本仍说"锁还在"，
    白白挡住了一次正常的起跑。锁是文件系统事实，就去文件系统看。
    """
    for key in ("data_dir", "budget_dir"):
        value = account.get(key)
        if value and (Path(value) / "cdp_risk_lock.json").exists():
            return True
    return False


def cmd_status() -> int:
    cfg = load_config()
    items = accounts_mod.registry(cfg)
    if not items:
        print("config.yaml 里没有 accounts 段")
        return 1

    print(f"{'账号':<4}{'服务':<7}{'状态':<10}{'今日':<10}{'目标':<6}{'下一步':<22}{'熔断/冷却'}")
    print("-" * 96)
    any_up = False
    for account in items:
        port = account["service_port"]
        pacing = _get(port, "/api/pacing/status")
        if pacing is None:
            state = "未启动" if account.get("enabled", True) else "已禁用"
            print(f"{account['id']:<4}:{port:<6}{state:<10}{'-':<10}{'-':<6}{'-':<22}-")
            continue
        any_up = True
        used = f"{pacing.get('used')}/{pacing.get('target')}"
        nxt = str(pacing.get("next_at") or "—")[:19]
        flag = pacing.get("halted_until") and f"熔断→{str(pacing['halted_until'])[:19]}" or (
            pacing.get("cooldown_until") and "冷却中" or "—"
        )
        state = "运行中" if pacing.get("running") else "已停止"
        print(f"{account['id']:<4}:{port:<6}{state:<10}{used:<10}{pacing.get('target'):<6}{nxt:<22}{flag}")
    if not any_up:
        print("\n没有任何账号的服务在跑。`python -m app.tools.multi start` 起全部。")
    return 0


def cmd_start(cap: int = 0, force: bool = False) -> int:
    """给每个 enabled 账号起一个独立服务进程（不同端口）。"""
    from app.tools import launch

    cfg = load_config()
    items = [a for a in accounts_mod.registry(cfg) if a.get("enabled", True)]
    if not items:
        print("没有 enabled 的账号")
        return 2

    rc = 0
    for account in items:
        port = account["service_port"]
        print(f"\n=== 账号 {account['id']} · 服务端口 {port} ===")
        argv = ["--account", account["id"], "--port", str(port), "--detach"]
        # 【2026-11-13 事故】环境锁着的时候**不许自动起跑**：
        #   当天 recover 探测失败 → 上游 6 秒后把锁写回 → 我们照样 start，
        #   结果是「页面显示已起跑、used 一直是 0」。正确动作是先 recover，不是点开始。
        if cap > 0 and not force and _risk_locked(account):
            print("检测到风控锁文件仍在：**跳过自动起跑**。先 recover，冷却后再起。")
            print("  python -m app.tools.recover --confirmed")
            rc = max(rc, 1)
            continue
        if cap > 0:
            argv += ["--cap", str(cap)]
        try:
            rc = max(rc, launch.main(argv))
        except SystemExit as exc:  # launch 用 SystemExit 报端口冲突
            print(f"账号 {account['id']} 启动失败：{exc}")
            rc = 1
    return rc


def cmd_stop() -> int:
    """停掉所有账号的服务（只杀本项目路径下的 python）。"""
    import psutil

    cfg = load_config()
    stopped = 0
    for account in accounts_mod.registry(cfg):
        port = account["service_port"]
        pid = _listener_pid(port)
        if not pid:
            print(f"账号 {account['id']}（:{port}）没有在跑")
            continue
        try:
            proc = psutil.Process(pid)
            cmdline = " ".join(proc.cmdline()).lower()
        except Exception:
            print(f"账号 {account['id']}（:{port}）PID={pid} 读不到命令行，跳过（不盲杀）")
            continue
        if str(PROJECT_ROOT).lower() not in cmdline and "uvicorn" not in cmdline:
            print(f"账号 {account['id']}（:{port}）占用者不像本项目，跳过")
            continue
        proc.terminate()
        print(f"账号 {account['id']}（:{port}）已停止（PID={pid}）")
        stopped += 1
    print(f"\n共停止 {stopped} 个实例")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    action = args[0] if args else "status"

    if action == "status":
        return cmd_status()
    if action == "start":
        cap = 0
        if "--cap" in args:
            cap = int(args[args.index("--cap") + 1])
        return cmd_start(cap, force="--force" in args)
    if action == "stop":
        return cmd_stop()

    print("用法：python -m app.tools.multi [status|start [--cap N]|stop]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
