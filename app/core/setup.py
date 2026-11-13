"""首次启动向导：把「改三处 YAML 路径」变成「页面上点三下」（2026-11-13）。

## 为什么这是"下载即用"的关键

README 写得再清楚，一个求职的人也不会去手改 `config.yaml` 里的
`chrome_profile` / `data_dir` / `chrome_exe` —— 那是开发者习惯，不是用户习惯。

所以把这三步搬进页面：**探测 Chrome → 选 profile 目录 → 点一下启动浏览器**，
程序负责写回配置。

## 安全边界

- 只改 `accounts[]` 第一个账号的四个字段（`chrome_exe` / `chrome_profile` / `data_dir` / `cdp_port`）
  和顶层 `adapter_options.data_dir`
- 不碰 `pacing` / `quota` / `target`（那些是业务策略，向导没资格改）
- 路径必须能创建/存在，否则拒绝写入（宁可让人重选，不要写一个跑不起来的配置）
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_YAML = PROJECT_ROOT / "config.yaml"
EXAMPLE_YAML = PROJECT_ROOT / "config.example.yaml"

#: 常见 Chrome 安装位置（按优先级）
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Users\{user}\AppData\Local\Google\Chrome\Application\chrome.exe",
)


def detect_chrome() -> list[str]:
    """返回本机探测到的 Chrome 路径（去重、只留存在的）。"""
    import getpass

    found: list[str] = []
    for pattern in CHROME_CANDIDATES:
        path = pattern.format(user=getpass.getuser())
        if Path(path).exists() and path not in found:
            found.append(path)
    return found


def _parse_accounts(text: str) -> list[dict[str, str]]:
    """粗略解析 accounts 段里的 id（只取够用的信息，不做完整 YAML 解析）。"""
    out: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    in_accounts = False
    for line in text.splitlines():
        if line.startswith("accounts:"):
            in_accounts = True
            continue
        if in_accounts and line and not line.startswith((" ", "\t", "#")):
            break  # 进入下一个顶层键
        if not in_accounts:
            continue
        match = re.match(r"\s*-\s*id:\s*(\S+)", line)
        if match:
            current = {"id": match.group(1)}
            out.append(current)
            continue
        if current is not None:
            for key in ("chrome_profile", "data_dir", "chrome_exe", "cdp_port"):
                field = re.match(rf"\s*{key}:\s*(.+?)\s*(?:#.*)?$", line)
                if field:
                    current[key] = field.group(1).strip().strip('"\'')
    return out


def placeholder_like(value: str) -> bool:
    """占位符 / 空值 → 说明还没配置过。"""
    text = str(value or "").strip().strip('"\'')
    return not text or "<you>" in text or "<用户名>" in text or text.upper() in {"CHROME.EXE", ""}


def status() -> dict[str, Any]:
    """向导需要什么？前端据此决定是否显示。"""
    exists = CONFIG_YAML.exists()
    text = CONFIG_YAML.read_text(encoding="utf-8") if exists else ""
    accounts = _parse_accounts(text) if text else []
    first = accounts[0] if accounts else {}

    missing: list[str] = []
    if not exists:
        missing.append("config.yaml 还不存在")
    else:
        for key, label in (
            ("chrome_exe", "Chrome 路径"),
            ("chrome_profile", "专用浏览器资料目录"),
            ("data_dir", "上游工作目录"),
        ):
            if placeholder_like(first.get(key, "")):
                missing.append(f"{label}没填")

    profile = str(first.get("chrome_profile") or "").strip().strip('"\'')
    return {
        "needed": bool(missing),
        "missing": missing,
        "config_exists": exists,
        "account_id": first.get("id") or "",
        "current": {
            "chrome_exe": first.get("chrome_exe", ""),
            "chrome_profile": profile,
            "data_dir": first.get("data_dir", ""),
            "cdp_port": first.get("cdp_port", ""),
        },
        "detected_chrome": detect_chrome(),
        "profile_exists": bool(profile) and Path(profile).exists(),
        # 默认建议值：跟项目同级的目录，避免用户去想路径
        "suggest_profile": str(Path.home() / "chrome-jobpilot"),
        "suggest_data_dir": str(Path.home() / ".boss-agent-jobpilot"),
    }


def _set_field(block: str, key: str, value: str, indent: str = "    ", *, quote: bool = True) -> str:
    literal = f'"{value}"' if quote else value
    pattern = re.compile(rf"^(\s*){key}:\s*.*$", re.M)
    if pattern.search(block):
        return pattern.sub(lambda m: f"{m.group(1)}{key}: {literal}", block, count=1)
    return block.rstrip() + f"\n{indent}{key}: {literal}\n"


def apply(chrome_exe: str, profile_dir: str, data_dir: str, cdp_port: int = 9333) -> dict[str, Any]:
    """把三处路径写进 config.yaml。返回 `{ok, error?, written}`。"""
    chrome_exe = str(chrome_exe or "").strip()
    profile_dir = str(profile_dir or "").strip()
    data_dir = str(data_dir or "").strip()

    if not chrome_exe or not Path(chrome_exe).exists():
        return {"ok": False, "error": f"Chrome 路径不存在：{chrome_exe or '(空)'}"}
    if not profile_dir:
        return {"ok": False, "error": "专用浏览器资料目录不能为空"}
    if not data_dir:
        return {"ok": False, "error": "上游工作目录不能为空"}

    # 目录不存在就创建（这两个都是"放数据"的地方，创建是安全的）
    try:
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        Path(data_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"目录创建失败：{exc}"}

    if not CONFIG_YAML.exists():
        if not EXAMPLE_YAML.exists():
            return {"ok": False, "error": "config.yaml 与 config.example.yaml 都不存在"}
        shutil.copyfile(EXAMPLE_YAML, CONFIG_YAML)

    text = CONFIG_YAML.read_text(encoding="utf-8")
    start = text.find("accounts:")
    if start == -1:
        return {"ok": False, "error": "config.yaml 里没有 accounts 段"}

    head, block = text[:start], text[start:]
    # 只改**第一个**账号：向导服务的是"刚下载下来的人"，单账号足够
    first_id_end = block.find("- id:", block.find("- id:") + 1) if block.count("- id:") > 1 else len(block)
    first_part, rest = block[:first_id_end], block[first_id_end:]

    for key, value in (
        ("chrome_exe", chrome_exe.replace("\\", "/")),
        ("chrome_profile", profile_dir.replace("\\", "/")),
        ("data_dir", data_dir.replace("\\", "/")),
    ):
        first_part = _set_field(first_part, key, value)
    # 端口是数字，不能加引号（YAML 会当字符串，后续 int() 才报错）
    first_part = _set_field(first_part, "cdp_port", str(int(cdp_port)), quote=False)

    # 顶层 adapter_options.data_dir 也同步（单账号直跑时会用到）
    head = re.sub(
        r"(adapter_options:\n(?:\s+.*\n)*?\s+data_dir:\s*).*",
        lambda m: m.group(1) + f"'{data_dir.replace(chr(92), '/')}'",
        head,
        count=1,
    )

    CONFIG_YAML.write_text(head + first_part + rest, encoding="utf-8")
    return {
        "ok": True,
        "written": {"chrome_exe": chrome_exe, "chrome_profile": profile_dir,
                    "data_dir": data_dir, "cdp_port": int(cdp_port)},
    }


def launch_browser(chrome_exe: str, profile_dir: str, cdp_port: int, url: str) -> dict[str, Any]:
    """按配置启动专用浏览器（带调试端口 + 独立 profile）。

    为什么由程序启动：手敲那条长命令是"下载即用"的第二个门槛。
    """
    if not chrome_exe or not Path(chrome_exe).exists():
        return {"ok": False, "error": "Chrome 路径无效"}
    args = [
        chrome_exe,
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={int(cdp_port)}",
        f"--user-data-dir={profile_dir}",
        url or "https://www.zhipin.com/web/geek/chat",
    ]
    try:
        subprocess.Popen(args, close_fds=True)  # noqa: S603
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"启动失败：{type(exc).__name__} {exc}"}
    return {
        "ok": True,
        "hint": f"已启动。请在弹出的窗口里登录你的求职账号，然后回到本页点「我登录好了」。",
        "verify": f"http://127.0.0.1:{int(cdp_port)}/json/version",
    }


def cdp_alive(port: int, timeout: float = 3.0) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/json/version", timeout=timeout) as resp:  # noqa: S310
            json.loads(resp.read().decode("utf-8"))
        return True
    except Exception:  # noqa: BLE001
        return False
