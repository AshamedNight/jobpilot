"""改 CDP 调试端口：一处改，三处必须同步（2026-11-13）。

## 为什么需要工具

调试端口不止记在 `config.yaml` 里，**上游自己的工作目录也有一份**：

| 位置 | 字段 |
|---|---|
| `config.yaml` | `accounts[].cdp_port` |
| `<data_dir>/config.json` | `cdp_url`（`http://127.0.0.1:9222`）|
| Chrome 启动参数 | `--remote-debugging-port=…` |

只改一处会**静默失效**：CLI 连的还是老端口 → 表现为
`BROWSER_SESSION_NOT_FOUND` 或直接超时，看起来像"浏览器没登录"。
这种错很难猜，所以做成一条命令改两处，第三处打印给你复制。

## 用法

```bash
python -m app.tools.rebind_cdp --port 9333            # 改默认账号
python -m app.tools.rebind_cdp --port 9334 --account a
```

改完必须**重启浏览器**（新端口只对新进程生效），命令会打印出来。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from app.config import load_config
from app.core import accounts as accounts_mod

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_YAML = PROJECT_ROOT / "config.yaml"


def update_yaml(account_id: str, port: int) -> bool:
    """把 `accounts` 里该账号的 cdp_port 改掉（只改那一行，不动别的）。"""
    text = CONFIG_YAML.read_text(encoding="utf-8")
    # 定位该账号段落：从 `- id: <id>` 到下一个 `- id:` 或文件尾
    pattern = re.compile(rf"(-\s*id:\s*{re.escape(account_id)}\b)(.*?)(?=\n\s*-\s*id:|\Z)", re.S)
    match = pattern.search(text)
    if not match:
        print(f"config.yaml 里找不到账号 {account_id}")
        return False
    block = match.group(2)
    if re.search(r"^\s*cdp_port:\s*\d+", block, re.M):
        new_block = re.sub(r"^(\s*)cdp_port:\s*\d+", rf"\g<1>cdp_port: {port}", block, count=1, flags=re.M)
    else:
        new_block = block.rstrip() + f"\n    cdp_port: {port}\n"
    text = text[: match.start(2)] + new_block + text[match.end(2) :]
    CONFIG_YAML.write_text(text, encoding="utf-8")
    return True


def update_upstream(data_dir: Path, port: int) -> bool:
    """改上游 `config.json` 的 `cdp_url`。"""
    path = data_dir / "config.json"
    if not path.exists():
        print(f"上游配置不存在：{path}（先让 CLI 跑一次，它会自己生成）")
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"上游配置不是合法 JSON：{path}")
        return False
    payload["cdp_url"] = f"http://127.0.0.1:{port}"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def chrome_command(account: dict, port: int) -> str:
    return (
        f'"{account.get("chrome_exe") or "chrome.exe"}" '
        f"--remote-debugging-address=127.0.0.1 --remote-debugging-port={port} "
        f'--user-data-dir="{account["chrome_profile"]}"'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="改 CDP 调试端口（同步 config.yaml + 上游 config.json）")
    parser.add_argument("--port", type=int, required=True, help="新的调试端口，例如 9333")
    parser.add_argument("--account", default=None, help="账号 id（默认用活动账号）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将改什么")
    args = parser.parse_args(argv)

    if args.account:
        import os

        os.environ["JOBPILOT_ACCOUNT"] = args.account
    cfg = load_config()
    account = accounts_mod.active_account(cfg)
    if not account:
        print("没有活动账号：先 `accounts switch <id>` 或加 --account")
        return 2

    if args.port < 1024 or args.port > 65535:
        print("端口要在 1024–65535 之间")
        return 2

    old_port = int(account["cdp_port"])
    print(f"账号 {account['id']}：cdp_port {old_port} → {args.port}")
    print(f"  1) config.yaml            accounts[{account['id']}].cdp_port")
    print(f"  2) {Path(account['data_dir']) / 'config.json'}  cdp_url")
    print()

    if args.dry_run:
        print("（演练）没改任何文件")
    else:
        ok_yaml = update_yaml(account["id"], args.port)
        ok_up = update_upstream(Path(account["data_dir"]), args.port)
        if not (ok_yaml and ok_up):
            print("\n有文件没改成：修好再重跑（半套改动会让 CLI 连错端口）")
            return 3
        print("已改好两处配置。")

    print("\n下一步（**必须**重启专用浏览器，新端口只对新进程生效）：")
    print(f"  {chrome_command(account, args.port)}")
    print("\n然后确认：")
    print(f"  curl http://127.0.0.1:{args.port}/json/version")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
