"""环境体检：把"环境干不干净"里可观测的部分摊出来。

## 为什么需要它

`ENVIRONMENT_RISK`（code 37）是**环境门控**，绑的不是账号而是环境特征。撞了之后
我们只能猜"到底哪一项惹的"。这份体检把能观测的都列出来，方便**换环境前后对比**：

- 出口 IP（换热点/重拨号后应该变）
- Chrome 版本（升级会改 UA 与指纹）
- profile 路径与创建时间（新 profile = 新指纹）
- 锁文件状态与写入时间（判断是不是刚被写）
- 最近的 risk 记录（今天第几次）

## 用法

```bash
python -m app.tools.envcheck              # 全量（会查一次出口 IP）
python -m app.tools.envcheck --no-net     # 不联网，只看本地
```

注意：查出口 IP 走的是第三方服务（ifconfig.me），**不碰 BOSS**，不消耗请求预算。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from app.config import load_config
from app.core import accounts as accounts_mod

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _fmt_ts(path: Path) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
    except OSError:
        return "—"


def public_ip(timeout: float = 6.0) -> str:
    try:
        with urllib.request.urlopen("https://ifconfig.me/ip", timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8", "replace").strip()
    except Exception as exc:  # noqa: BLE001
        return f"查询失败（{type(exc).__name__}）"


def chrome_version(exe: str) -> str:
    """读 Chrome 版本（文件版本信息，不启动浏览器）。"""
    if not exe or not Path(exe).exists():
        return "找不到 chrome.exe"
    try:
        out = subprocess.run(  # noqa: S603, S607
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{exe}').VersionInfo.ProductVersion"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return out or "读取失败"
    except Exception:  # noqa: BLE001
        return "读取失败"


def lock_state(data_dir: Path) -> str:
    path = data_dir / "cdp_risk_lock.json"
    if not path.exists():
        return "无锁 ✅"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        reason = payload.get("reason") or payload.get("message") or ""
        return f"**有锁**（写入于 {_fmt_ts(path)}）{reason[:40]}"
    except Exception:  # noqa: BLE001
        return f"**有锁**（写入于 {_fmt_ts(path)}，内容读不出）"


def recent_risks(db_path: Path, limit: int = 5) -> list[str]:
    import sqlite3

    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT started_at, error FROM runs WHERE error LIKE '%RISK%' "
            "ORDER BY run_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [f"{r[0][:19]}  {(r[1] or '')[:48]}" for r in rows]
    except Exception:  # noqa: BLE001
        return []


def report(account: dict, *, net: bool) -> None:
    print(f"账号 {account['id']} · {account['name']}")
    print("-" * 60)
    if net:
        print(f"出口 IP        : {public_ip()}")
    print(f"Chrome 版本    : {chrome_version(account.get('chrome_exe') or '')}")
    profile = Path(account["chrome_profile"])
    print(f"profile        : {profile}")
    print(f"                 {'存在' if profile.exists() else '不存在'}，创建于 {_fmt_ts(profile) if profile.exists() else '—'}")
    upstream_cdp = ""
    try:
        cfg_json = json.loads((Path(account["data_dir"]) / "config.json").read_text(encoding="utf-8"))
        upstream_cdp = str(cfg_json.get("cdp_url") or "")
    except Exception:  # noqa: BLE001
        pass
    expect = f"http://127.0.0.1:{account['cdp_port']}"
    if upstream_cdp and upstream_cdp != expect:
        print(f"调试端口       : {account['cdp_port']}  ❌ 与上游 cdp_url 不一致（{upstream_cdp}）")
        print("                 改端口请用 `python -m app.tools.rebind_cdp --port N`（两处要同步）")
    else:
        print(f"调试端口       : {account['cdp_port']}（上游 cdp_url 一致 ✅）")
    print(f"上游目录       : {account['data_dir']}")
    print(f"风控锁         : {lock_state(Path(account['data_dir']))}")
    budget = account.get("budget_dir")
    if budget:
        print(f"预算目录锁     : {lock_state(Path(budget))}")
    db = Path(account["db"])
    print(f"数据库         : {db}（{'存在' if db.exists() else '不存在'}）")
    risks = recent_risks(db)
    if risks:
        print("最近 risk 记录 :")
        for line in risks:
            print(f"                 {line}")
    else:
        print("最近 risk 记录 : 无")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="环境体检（判断环境是否干净）")
    parser.add_argument("--no-net", action="store_true", help="不查出口 IP")
    parser.add_argument("--account", default=None, help="只看指定账号")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.account:
        os.environ["JOBPILOT_ACCOUNT"] = args.account
        cfg = load_config()

    account = accounts_mod.active_account(cfg)
    if not account:
        items = accounts_mod.registry(cfg)
        if not items:
            print("没有可用的账号配置")
            return 1
        account = items[0]
        print("（没有活动账号，默认体检第一个）\n")

    report(account, net=not args.no_net)
    print()
    print("判断方法：换环境（热点/重拨号/新 profile）前后各跑一次，看哪几项变了。")
    print("真正会变环境的只有：出口 IP、profile（指纹）、Chrome 版本。换账号不会变环境。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
