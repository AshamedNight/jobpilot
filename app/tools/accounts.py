"""账号管理 CLI（闭环 3）。

```bash
python -m app.tools.accounts list        # 列出所有账号 + 当前是哪个
python -m app.tools.accounts current     # 只看当前
python -m app.tools.accounts switch a    # 切到 a（只写指针，不重启）
```

**切换只是写指针**，真正生效要重启服务：
`python -m app.tools.launch --detach --cap 15`（它按指针装环境），
或者等「每 5 分钟自愈」的守护把服务拉起。

为什么不自动重启：切换账号是**停机操作**。自动重启可能正好撞上 pacer 正在发的那一条，
而且用户往往需要先在那个 Chrome profile 里登录好，才谈得上切换生效。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.config import load_config
from app.core import accounts

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def cmd_list() -> int:
    cfg = load_config()
    items = accounts.registry(cfg)
    if not items:
        print("config.yaml 里没有 accounts 段。")
        return 1
    active = accounts.active_account(cfg)
    active_id = (active or {}).get("id")
    print(f"共 {len(items)} 个账号（* = 当前）：\n")
    for account in items:
        mark = "*" if account["id"] == active_id else " "
        print(f" {mark} {accounts.describe(account)}")
        print(f"    库     : {account['db']}")
        print(f"    上游目录: {account['data_dir']}")
        print(f"    profile: {account['chrome_profile']}")
    if not active_id:
        print("\n当前没有指针：启动时按 config.yaml 的 adapter_options.data_dir 走。")
    else:
        print(f"\n切换后要重启服务才生效：python -m app.tools.launch --detach --cap 15")
    return 0


def cmd_current() -> int:
    cfg = load_config()
    account = accounts.active_account(cfg)
    if not account:
        print("当前账号：未指定（走 config.yaml 默认）")
        return 0
    print(f"当前账号：{account['id']} · {account['name']}")
    print(f"  库      : {account['db']}")
    print(f"  上游目录: {account['data_dir']}")
    print(f"  调试端口: {account['cdp_port']}")
    return 0


def cmd_switch(account_id: str) -> int:
    cfg = load_config()
    target = accounts.find(cfg, account_id)
    if target is None:
        ids = "、".join(a["id"] for a in accounts.registry(cfg)) or "（无）"
        print(f"没有这个账号：{account_id}。可选：{ids}")
        return 1

    # 切换前先确认 profile 存在 —— 不存在说明还没在那个 profile 里登录过，
    # 切过去只会得到"未登录"，不如现在就拦住。
    if not Path(target["chrome_profile"]).exists():
        print(f"❌ profile 目录不存在：{target['chrome_profile']}")
        print("   先启动一次专用浏览器并登录，再切过来。")
        return 1

    accounts.write_pointer(account_id)
    print(f"✅ 已切到 {account_id} · {target['name']}")
    print("   下一步：python -m app.tools.launch --detach --cap 15")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    action = args[0] if args else "list"
    if action == "list":
        return cmd_list()
    if action == "current":
        return cmd_current()
    if action == "switch":
        if len(args) < 2:
            print("用法：python -m app.tools.accounts switch <id>")
            return 2
        return cmd_switch(args[1])
    print("用法：python -m app.tools.accounts [list|current|switch <id>]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
