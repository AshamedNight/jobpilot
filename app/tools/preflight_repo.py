"""发版前自检：确认"干净仓库"真的干净（2026-11-13）。

## 为什么需要这个

把项目当简历作品发出去，最怕的不是功能不全，是**把本机数据一起推上去了**：
数据库里有 HR 姓名、公司、微信号；日志里有绝对路径；profile 目录里有 cookie。

这些一旦进了公开仓库，删掉 commit 也没用（fork、缓存、镜像到处都是）。

所以做成一条命令：扫描**被 git 跟踪的文件**，命中就报错。

## 检查项

| 检查 | 为什么 |
|---|---|
| 本机用户名 / 绝对路径 | 泄露个人环境，也不好复现 |
| 数据库 / profile / 日志文件 | 里面是真实会话与凭据 |
| 手机号、微信号样式的字符串 | 真实联系人信息 |
| `.env` 被跟踪 | 可能含 LLM key |
| `config.yaml` 被跟踪 | 含本机绝对路径（应只提交 example） |

## 用法

```bash
python -m app.tools.preflight_repo          # 只检查
python -m app.tools.preflight_repo --fix-hint   # 附带修复建议
```

退出码：0 干净 / 1 有问题。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 这些路径出现在跟踪列表里就直接判失败（不是内容问题，是文件本身不该在）
FORBIDDEN_TRACKED = (
    ".venv",
    "data/",
    "config.yaml",
    ".env",
    "chrome-boss",
    ".boss-agent",
    "pacer_state",
    "probe_budget",
)

#: 内容里的敏感样式
PATTERNS = {
    "Windows 绝对路径": re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\\/\s\"']+"),
    "手机号": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "微信号样式的串": re.compile(r"微信号[：:]\s*[A-Za-z0-9_-]{6,}"),
}

#: 允许出现在文档示例里的占位符
ALLOWED_PLACEHOLDERS = ("<you>", "<用户名>", "C:\\Users\\<")

#: 公知的假数据（测试夹具用），不算泄露。只放"一看就是编的"值。
ALLOWED_SAMPLES = frozenset({"13800138000", "wxid_lawyer2026"})

#: 这些目录里出现手机号/微信号样式的串属于测试夹具
SAMPLE_DIRS = ("tests/",)


def tracked_files() -> list[str]:
    try:
        out = subprocess.run(  # noqa: S603, S607
            ["git", "ls-files"], cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        ).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:  # noqa: BLE001
        return []



def scan_text(text: str, path: str = "") -> list[str]:
    """扫描一段文本里的敏感样式。纯函数，方便单测。"""
    problems: list[str] = []
    is_sample_file = any(path.startswith(prefix) for prefix in SAMPLE_DIRS)
    for name, pattern in PATTERNS.items():
        if is_sample_file and name in ("手机号", "微信号样式的串"):
            continue  # 测试夹具不算泄露
        for match in pattern.finditer(text):
            snippet = match.group(0)
            if snippet in ALLOWED_SAMPLES:
                continue
            if any(token in snippet for token in ALLOWED_PLACEHOLDERS):
                continue
            line_start = text.rfind(chr(10), 0, match.start()) + 1
            line_end = text.find(chr(10), match.end())
            line = text[line_start: line_end if line_end != -1 else len(text)]
            if "<you>" in line or "<用户名>" in line:
                continue
            problems.append(f"{name}：{path} → {snippet[:40]}")
    return problems


def check() -> list[str]:
    problems: list[str] = []
    files = tracked_files()
    if not files:
        return ["读不到 git 跟踪列表（不是 git 仓库？）"]

    for path in files:
        lowered = path.lower()
        for bad in FORBIDDEN_TRACKED:
            if bad in lowered:
                # 允许 example 模板
                if bad == "config.yaml" and path.endswith("config.example.yaml"):
                    continue
                # `.env.example` 是模板，故意提交；只有真正的 `.env` 不许入库
                if bad == ".env" and path.endswith(".env.example"):
                    continue
                problems.append(f"不该被跟踪的文件：{path}")
                break

    for path in files:
        full = PROJECT_ROOT / path
        if not full.exists() or full.suffix.lower() in {".png", ".jpg", ".ico", ".zip", ".exe"}:
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        problems.extend(scan_text(text, path))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发版前自检：确认仓库里没有本机数据")
    parser.add_argument("--fix-hint", action="store_true", help="附修复建议")
    args = parser.parse_args(argv)

    problems = check()
    if not problems:
        print("✅ 干净：被跟踪的文件里没有发现本机数据或敏感样式")
        print(f"   跟踪文件数：{len(tracked_files())}")
        return 0

    print(f"❌ 发现 {len(problems)} 处问题：")
    for item in problems[:40]:
        print(f"  - {item}")
    if len(problems) > 40:
        print(f"  …还有 {len(problems) - 40} 处")

    if args.fix_hint:
        print("\n修复建议：")
        print("  git rm -r --cached .venv2        # 虚拟环境不入库（磁盘文件保留）")
        print("  git rm --cached config.yaml      # 只提交 config.example.yaml")
        print("  git rm --cached data/*           # 数据库/日志/profile 一律不入库")
        print("  若历史提交里已存在，发布前建议新建一个干净仓库（单次提交），不要推送旧历史")
        print("  彻底清洗历史：git filter-repo --path .venv2 --path config.yaml --invert-paths")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
