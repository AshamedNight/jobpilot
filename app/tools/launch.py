"""一键启动：端口自愈 → 起服务 → 等就绪 → 起跑 → 开页面。

## 为什么用 Python 而不是 PowerShell

原 `start-jobpilot.ps1` 每天要人做四件事：判断端口有没有被占、启动、记得点"开始"、开页面。
其中"端口被占"今天就真实卡了一次（`[Errno 10048]`，因为上一个实例没关），
而且 PowerShell 5.1 读不带 BOM 的 UTF-8 脚本会把中文注释读成乱码。

这个启动器把四件事合成一条命令，且**只杀属于自己的进程**：
8010 的占用者必须是"本项目路径下的 python"，否则报错退出。别人的进程一律不碰。

## 用法

```bash
# 日常：起服务 + 起跑 + 打开页面
.venv2\\Scripts\\python.exe -m app.tools.launch --cap 15 --open

# 只看服务，不自动起跑
.venv2\\Scripts\\python.exe -m app.tools.launch

# 后台起（不阻塞当前终端），稍后自己看页面
.venv2\\Scripts\\python.exe -m app.tools.launch --cap 15 --detach
```
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PORT = 8010

#: 这些环境变量是"真机跑"的前提；没设就补上。
#: 【2026-11-13 发布】原来这里是写死某个用户的家目录，换个用户就不存在。
#: 改成从 Path.home() 推——发布给别人用时，默认值至少指向一个真实存在的家目录。
_HOME = Path.home()

DEFAULT_ENV = {
    "JOBPILOT_BOSS_CRED_DIR": str(_HOME / ".boss-agent-jobpilot-new"),
    "JOBPILOT_BOSS_BUDGET_DIR": str(_HOME / ".boss-agent-test"),
    "JOBPILOT_CHROME_EXE": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "JOBPILOT_CHROME_PROFILE_DIR": str(_HOME / "chrome-boss"),
}


def _base(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _api(path: str, port: int, method: str = "GET", timeout: float = 5.0):
    req = urllib.request.Request(_base(port) + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 固定回环地址
        return json.loads(resp.read().decode("utf-8"))


def _listener_pid(port: int) -> int | None:
    """找出监听指定端口的 PID。

    【2026-11-12 修】不能只靠 `psutil.net_connections()`：这台机器上它**看不到**
    8010 的 LISTENING 套接字（返回空），于是"端口自愈"静默失效，
    最终仍然以 `[Errno 10048]` 失败。改为 `netstat -ano` 主判据 + psutil 兜底。
    """
    try:
        out = subprocess.run(  # noqa: S603, S607 - 固定参数、无 shell
            ["netstat", "-ano"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        ).stdout or ""
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
                return int(parts[4])
    except Exception:
        pass
    try:
        import psutil

        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr and conn.laddr.port == port and conn.status == "LISTENING" and conn.pid:
                return int(conn.pid)
    except Exception:
        pass
    return None


def _is_own_process(pid: int) -> bool:
    """占用者是不是"本项目路径下的 python"。

    这是**安全边界**：8010 上可能是任何东西（比如另一个项目、或用户自己的脚本），
    盲杀别人的进程比启动失败更糟。
    """
    try:
        import psutil
    except ImportError:
        return False
    try:
        proc = psutil.Process(pid)
    except Exception:
        return False
    if "python" not in (proc.name() or "").lower():
        return False
    try:
        cmdline = " ".join(proc.cmdline())
        cwd = proc.cwd()
    except Exception:
        return False
    root = str(PROJECT_ROOT).lower()
    return root in cmdline.lower() or root in cwd.lower()


def _ensure_port_free(port: int) -> None:
    pid = _listener_pid(port)
    if pid is None:
        return
    if not _is_own_process(pid):
        raise SystemExit(
            f"端口 {port} 被 PID={pid} 占用，且不是本项目的 python。\n"
            f"请先手动处理（脚本不会盲杀别人的进程）。"
        )
    print(f"端口 {port} 被本项目上一个实例占用（PID={pid}），收掉它…")
    try:
        import psutil

        psutil.Process(pid).terminate()
        for _ in range(20):
            if _listener_pid(port) is None:
                break
            time.sleep(0.3)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"结束旧实例失败：{type(exc).__name__}") from exc


def _wait_ready(port: int, timeout_s: float = 40.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            _api("/api/status", port, timeout=2.0)
            return True
        except Exception:
            time.sleep(1.0)
    return False



def format_start_result(resp: dict) -> str:
    """把 `/api/pacing/start` 的返回翻译成人话。

    【2026-11-13 事故】原实现直接读 `resp["status"]`，**无视 `ok:false`**：
    风控锁在时接口返回 `{"ok": false, "error": "..."}`（没有 status 字段），
    于是打印出「已起跑: used=None target=None next=None」——
    看起来像成功了，实际上一条都不会发。误导成本极高：人会以为今天在跑，
    半天后才发现 used 还是 0。
    """
    if not resp.get("ok"):
        error = str(resp.get("error") or "未知原因")
        hint = ""
        if "风控" in error or "RISK" in error.upper():
            hint = "\n  → 先跑 `python -m app.tools.recover --confirmed`，冷却 30-60 分钟再起"
        return f"起跑被拒绝：{error}{hint}"

    status = resp.get("status") or {}
    if not status:
        return "起跑已受理（接口没返回状态，去页面上看 used 是否在涨）"
    return (
        f"已起跑：used={status.get('used')} target={status.get('target')} "
        f"next={status.get('next_at')} ({status.get('next_reason')})"
    )

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="jobpilot 一键启动")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--account", default=None, help="指定账号 id（覆盖 data/active_account.json 指针）")
    parser.add_argument("--cap", type=int, default=0, help="起跑时今天的硬上限（0 = 不限制）")
    parser.add_argument("--open", action="store_true", help="就绪后打开控制台页面")
    parser.add_argument("--detach", action="store_true", help="后台起服务，本命令立即返回")
    parser.add_argument("--no-start", action="store_true", help="只起服务，不调 /api/pacing/start")
    args = parser.parse_args(argv)

    # 0) 多账号并行（闭环 3）：`--account` 直接指定，优先于指针文件。
    if args.account:
        os.environ["JOBPILOT_ACCOUNT"] = str(args.account)

    # 1) 账号（闭环 3）：指针里指定的账号优先，它决定库 / 上游目录 / profile / 端口。
    #    没有指针就退回 DEFAULT_ENV（老行为不变）。
    account_label = "默认（config.yaml）"
    try:
        from app.config import load_config
        from app.core import accounts as accounts_mod

        cfg = load_config()
        account = accounts_mod.active_account(cfg)
        if account:
            os.environ.update(accounts_mod.env_for(account))
            account_label = f"{account['id']} · {account['name']}"
    except Exception as exc:  # noqa: BLE001 - 账号配置坏了不该拦住启动，但要吼一声
        print(f"账号配置读取失败（按默认启动）：{type(exc).__name__} {exc}")

    # 2) 环境：缺的补上，已设的不覆盖（账号已设的保持）
    os.environ.setdefault("JOBPILOT_DB", str(PROJECT_ROOT / "data" / "jobpilot-new.db"))
    for key, value in DEFAULT_ENV.items():
        os.environ.setdefault(key, value)

    print(f"账号   : {account_label}")

    # 3) 端口自愈
    _ensure_port_free(args.port)

    # 4) 起服务
    cmd = [
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", "127.0.0.1", "--port", str(args.port), "--workers", "1",
    ]
    print("启动：", " ".join(cmd[1:]))
    if args.detach:
        subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=dict(os.environ))  # noqa: S603
        child = None
    else:
        child = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=dict(os.environ))  # noqa: S603

    if not _wait_ready(args.port):
        print("服务 40 秒内没起来。看上面的报错。")
        return 2

    status = _api("/api/pacing/status", args.port)
    print(f"服务就绪：day={status.get('day')} used={status.get('used')} target={status.get('target')}")

    # 5) 起跑
    if not args.no_start:
        path = f"/api/pacing/start?cap={args.cap}" if args.cap > 0 else "/api/pacing/start"
        try:
            resp = _api(path, args.port, method="POST")
            print(format_start_result(resp))
        except Exception as exc:  # noqa: BLE001
            print(f"起跑失败（服务本身是好的）：{type(exc).__name__} {exc}")

    # 6) 开页面
    if args.open:
        webbrowser.open(_base(args.port))

    if child is None:
        return 0
    print("按 Ctrl+C 停止服务。")
    try:
        return child.wait()
    except KeyboardInterrupt:
        child.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
