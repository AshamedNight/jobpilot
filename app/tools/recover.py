"""命中风控后的恢复工具（闭环 1 运维口）。

**为什么要有这个脚本**

风控会同时留下两样东西，而且必须按顺序处理：

1. 上游本地锁 `cdp_risk_lock.json`（**两处**：隔离目录 + 共享预算目录）
2. 我们自己的 24h 熔断 `data/pacer_state.json` 里的 `halted_until`

只清第 1 条 → 一启动就被自己的熔断拦住，容易误判成"清了还是不发"；
只清第 2 条 → 上游每条请求都被本地拒，`found=0`，更迷惑。
而且**顺序不能反**：锁没清就启动 pacer，本轮会立刻再写一次 24h 熔断。

这个脚本把"人工确认 → 清锁 → 单页探测 → 归档熔断"串起来，**不启动任何东西**。

**它不做的事（故意的）**

- 不点 `pacing/start`：启动是人的决定，不是脚本的副作用。
- 不在探测失败时"再试一次"：平台刚拒过，重试只是在教它记住你。
- 不猜平台侧是否恢复：只认一次单页 `search` 的返回值。

用法（在项目根目录）::

    python -m app.tools.recover                # 只体检，不动任何文件
    python -m app.tools.recover --confirmed    # 确认已在官方页面手动验证过，才允许清锁+探测
    python -m app.tools.recover --confirmed --dry-run   # 演练：打印将做什么，不落手

退出码：0 恢复完成 / 2 探测仍被拒（锁可能已回写）/ 3 配置或环境问题 / 4 未确认。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

RISK_CODES = {"ACCOUNT_RISK", "ENVIRONMENT_RISK", "ENVIRONMENT_RISK_LOCKED", "LOGIN_RISK_CONTROL"}


# --------------------------------------------------------------------------- #
# 路径推导：优先用上游自己的函数，别硬编码
# --------------------------------------------------------------------------- #
def _data_dir(cfg: dict[str, Any]) -> Path:
    raw = ((cfg.get("adapter_options") or {}).get("data_dir")) or "~/.boss-agent-test/"
    return Path(str(raw)).expanduser().resolve()


def _budget_dir(data_dir: Path) -> Path:
    try:
        from boss_agent_cli.api.request_budget import budget_directory  # noqa: PLC0415

        return Path(budget_directory(data_dir))
    except Exception:  # 上游换实现时不阻塞恢复：退回"同目录"假设
        return data_dir


def _lock_path(directory: Path) -> Path:
    try:
        from boss_agent_cli.api.cdp_risk_lock import lock_path  # noqa: PLC0415

        return Path(lock_path(directory))
    except Exception:
        return directory / "cdp_risk_lock.json"


def _state_path(db_path: Path) -> Path:
    return db_path.parent / "pacer_state.json"


# --------------------------------------------------------------------------- #
# 环境体检：code 37 的三条常见诱因（来自社区排障清单）
# --------------------------------------------------------------------------- #
def check_environment() -> list[dict[str, str]]:
    """检查"环境门控"最容易被忽略的三件事。

    `ENVIRONMENT_RISK`（platform_code 37）判的是**环境**，社区排障清单里排在最前的
    几条恰好都不是代码：① 挂着代理/境外出口；② 没走专用 profile 手动登录；
    ③ 浏览器不是带 `--remote-debugging-port` 的那个实例。
    这里只读，不改任何系统设置。

    没有 `psutil` 时跳过浏览器命令行检查（其余照常），不把它变成硬依赖。
    """
    findings: list[dict[str, str]] = []

    # ① 代理：系统代理开着 + 存在 HTTP(S)_PROXY，都会让平台看到非本地出口
    proxy_enabled = "0"
    proxy_server = ""
    try:
        import winreg  # noqa: PLC0415 - 仅 Windows

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        try:
            proxy_enabled = str(winreg.QueryValueEx(key, "ProxyEnable")[0])
        except FileNotFoundError:
            proxy_enabled = "0"
        try:
            proxy_server = str(winreg.QueryValueEx(key, "ProxyServer")[0])
        except FileNotFoundError:
            proxy_server = ""
    except Exception:  # 非 Windows / 无权限：跳过这一条
        proxy_enabled = "?"

    env_proxy = {k: v for k, v in os.environ.items() if "proxy" in k.lower()}
    if proxy_enabled == "1" or env_proxy:
        findings.append(
            {
                "level": "warn",
                "text": f"代理开着（系统代理={proxy_enabled} {proxy_server} 环境变量={env_proxy or '无'}）："
                f"平台会看到非本地出口，是 code 37 的常见诱因，验证期建议关掉",
            }
        )

    # ② / ③ 浏览器实例：必须带 remote-debugging + 专用 profile
    try:
        import psutil  # noqa: PLC0415 - 可选依赖

        chrome_cmd: str | None = None
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                if (proc.info["name"] or "").lower() != "chrome.exe":
                    continue
                cmd = " ".join(proc.info["cmdline"] or [])
                if "--remote-debugging-port" in cmd and "--type=" not in cmd:
                    chrome_cmd = cmd
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if chrome_cmd is None:
            findings.append(
                {
                    "level": "warn",
                    "text": "没找到带 --remote-debugging-port 的 Chrome 主进程："
                    "CLI 会报 BROWSER_SESSION_NOT_FOUND，先按固定脚本重启专用浏览器",
                }
            )
        else:
            if "--user-data-dir" not in chrome_cmd:
                findings.append(
                    {
                        "level": "warn",
                        "text": "这个 Chrome 用的是默认 profile：自动化与日常浏览混在一起，"
                        "建议改用专用 --user-data-dir（例如 <你的家目录>\\chrome-boss）",
                    }
                )
            findings.append({"level": "ok", "text": f"浏览器命令行：{chrome_cmd[:180]}"})
    except ImportError:
        findings.append(
            {"level": "info", "text": "未安装 psutil：跳过浏览器命令行检查（pip install psutil 可开启）"}
        )

    if not findings:
        findings.append({"level": "ok", "text": "未发现代理或浏览器配置问题"})
    return findings


def render_environment(findings: list[dict[str, str]]) -> str:
    icon = {"ok": "OK  ", "warn": "注意", "info": "跳过"}
    lines = ["环境体检"]
    for item in findings:
        lines.append(f"  [{icon.get(item['level'], item['level'])}] {item['text']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 体检
# --------------------------------------------------------------------------- #
def inspect(cfg: dict[str, Any], db_path: Path) -> dict[str, Any]:
    data_dir = _data_dir(cfg)
    budget = _budget_dir(data_dir)
    locks = [_lock_path(data_dir), _lock_path(budget)]
    state = _state_path(db_path)

    payload: dict[str, Any] = {}
    if state.exists():
        try:
            payload = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}

    return {
        "data_dir": data_dir,
        "budget_dir": budget,
        "locks": [{"path": str(p), "exists": p.exists()} for p in locks],
        "state_path": state,
        "state_exists": state.exists(),
        "halted_until": payload.get("halted_until"),
        "cooldown_until": payload.get("cooldown_until"),
        "last_verdict": payload.get("last_verdict"),
        "last_error": payload.get("last_error"),
    }


def render(info: dict[str, Any]) -> str:
    lines = ["恢复体检", ""]
    for item in info["locks"]:
        flag = "在" if item["exists"] else "已清"
        lines.append(f"  上游锁 {flag}：{item['path']}")
    if info["state_exists"]:
        lines.append(f"  熔断文件：{info['state_path']}")
        lines.append(f"    halted_until = {info['halted_until']}")
        lines.append(f"    cooldown_until = {info['cooldown_until']}")
        lines.append(f"    last_verdict = {info['last_verdict']}（{info['last_error']}）")
    else:
        lines.append("  熔断文件：不存在（无本地熔断）")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 三步动作
# --------------------------------------------------------------------------- #
def clear_locks(locks: list[Path], *, dry_run: bool = False) -> list[str]:
    """删两处锁文件。**故意不用 `boss clean --risk-lock`**：那条命令走 safe-delete
    （某些环境退回收站失败就整体 fail-closed），而这里要的是确定的结果。"""
    removed: list[str] = []
    for path in locks:
        if not path.exists():
            continue
        if not dry_run:
            try:
                path.unlink()
            except OSError as exc:
                raise RuntimeError(f"删锁失败 {path}：{exc}") from exc
        removed.append(str(path))
    return removed


def probe(cfg: dict[str, Any], *, page: int) -> tuple[bool, str]:
    """一次有界单页搜索。返回 (是否放行, 说明)。

    只翻一页是刻意的：实测"同进程连翻 5 页"第 2 页就被判 `ENVIRONMENT_RISK`，
    而单页请求反复都正常。探测必须用最轻的一击。
    """
    from app.adapters.boss import BossAdapter  # 延迟 import：避免 --help 时也加载上游

    adapter = BossAdapter(cfg)
    jobs = adapter.search("法务", cities=(cfg.get("target") or {}).get("cities") or [], page=page)
    error = adapter.last_error or {}
    code = str(error.get("code") or "")
    normalized = str(error.get("normalized") or "")
    if code in RISK_CODES or normalized == "RISK_LOCKED":
        return False, f"仍被风控拒绝（{code or normalized}）：上游通常已重新写回本地锁"
    if error:
        return False, f"探测失败（{code or normalized}）：{error.get('detail') or ''}"[:300]
    return True, f"放行，单页返回 {len(jobs)} 条岗位"


def probe_runs(
    cfg: dict[str, Any], *, page: int, runs: int, gap_s: int
) -> tuple[bool, list[str], list[float]]:
    """在**同一个进程**里连做 `runs` 次单页探测，间隔 `gap_s` 秒。

    这是 2026-11-04 留下的那个"实验 A"：所有成功样本都是"独立进程 + 单页"，
    唯一失败样本是"同进程 + 5 页" —— 两个变量一起变了。这里固定"单页"，
    只让"同进程重复"这一个变量动，用来把它们拆开。

    任何一次失败立即停手（不重试），并把每次耗时返回，作为后续口径的依据。
    """
    import time  # noqa: PLC0415

    results: list[str] = []
    elapsed: list[float] = []
    for index in range(max(runs, 1)):
        if index and gap_s > 0:
            time.sleep(gap_s)
        started = time.monotonic()
        ok, detail = probe(cfg, page=page)
        cost = time.monotonic() - started
        elapsed.append(cost)
        results.append(f"第 {index + 1}/{runs} 次：{detail}（{cost:.0f}s）")
        if not ok:
            return False, results, elapsed
    return True, results, elapsed


def archive_state(state_path: Path, *, dry_run: bool = False) -> str | None:
    """把熔断文件改名归档（保留证据），不删除。"""
    if not state_path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = state_path.with_name(f"pacer_state.recovered-{stamp}.json.bak")
    if not dry_run:
        shutil.move(str(state_path), str(target))
    return str(target)


# --------------------------------------------------------------------------- #
# 重启专用浏览器（code 37 的标准动作，借自社区口径）
# --------------------------------------------------------------------------- #
def cdp_port(cfg: dict[str, Any]) -> int:
    """从 `<data_dir>/config.json` 的 `cdp_url` 取端口，取不到就 9222。"""
    try:
        raw = json.loads((_data_dir(cfg) / "config.json").read_text(encoding="utf-8"))
        url = str(raw.get("cdp_url") or "")
        return int(url.rsplit(":", 1)[-1].strip("/")) if ":" in url else 9222
    except Exception:
        return 9222


def _find_debug_chrome(port: int) -> tuple[Any, str] | tuple[None, str]:
    """找带 `--remote-debugging-port=<port>` 的 Chrome 主进程，返回 (proc, cmdline)。

    只认主进程（命令行里没有 `--type=`），避免把 renderer/GPU 子进程当成目标误杀。
    """
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return None, "缺 psutil，无法定位现有浏览器进程（pip install psutil）"

    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if (proc.info["name"] or "").lower() != "chrome.exe":
                continue
            cmd = " ".join(proc.info["cmdline"] or [])
            if f"--remote-debugging-port={port}" in cmd and "--type=" not in cmd:
                return proc, cmd
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None, f"没找到带 --remote-debugging-port={port} 的 Chrome 主进程"


def _arg_of(cmdline: str, flag: str) -> str:
    """从命令行里抠出 `--flag=value` 的 value（没有就空串）。"""
    for token in cmdline.split():
        if token.startswith(flag + "="):
            return token.split("=", 1)[1].strip('"')
    return ""


def _launch_cache_path(cache_path: Path | None = None) -> Path:
    """记住上次用的 exe + profile（电脑重启后进程没了，这是唯一的线索来源）。

    默认放在 `data/`（与 `pacer_state.json` 同目录），可用参数覆盖（测试用）。
    """
    return cache_path or (Path("data") / "chrome_launch.json")


def _load_launch(cache: Path) -> dict[str, str] | None:
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("exe") else None


def _save_launch(cache: Path, exe: str, profile: str, port: int) -> None:
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps({"exe": exe, "profile": profile, "port": port}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass  # 记不住不影响本次操作


def restart_browser(
    cfg: dict[str, Any], *, dry_run: bool = False, cache_path: Path | None = None
) -> tuple[bool, str]:
    """关掉带调试端口的那个 Chrome，再用**同一个专用 profile** 拉起来。

    为什么值得做成一步：code 37 是**环境门控**，社区排障的第一条修复就是"重启浏览器"。
    只清锁不重启，等于用同一个被判定过的会话继续敲。

    三条取值来源，按优先级：
    1. 正在运行的进程命令行（`--user-data-dir` + 可执行路径）——最可靠
    2. `JOBPILOT_CHROME_EXE` / `JOBPILOT_CHROME_PROFILE_DIR`
    3. `data/chrome_launch.json`（上次成功重启时记下来的）

    第 3 条是 2026-11-07 补的：电脑重启之后 Chrome 根本没在跑，"找不到进程就拒绝"
    等于让人每次重启电脑都得手敲一遍完整启动命令。
    """
    import os  # noqa: PLC0415

    port = cdp_port(cfg)
    cache = _launch_cache_path(cache_path)
    victim, cmdline = _find_debug_chrome(port)
    saved = _load_launch(cache) or {}

    exe = (os.environ.get("JOBPILOT_CHROME_EXE") or "").strip()
    profile = (os.environ.get("JOBPILOT_CHROME_PROFILE_DIR") or "").strip()
    if victim is not None:
        if not exe:
            try:
                exe = victim.exe()  # 进程自己的可执行路径，最可靠
            except Exception:  # noqa: BLE001 - 拿不到就退回别的来源
                exe = ""
        profile = profile or _arg_of(cmdline, "--user-data-dir")
    exe = exe or str(saved.get("exe") or "")
    profile = profile or str(saved.get("profile") or "")

    if not exe or not Path(exe).exists():
        return False, (
            f"定位不到 Chrome 可执行文件（读到 {exe!r}）。先手动起一次带调试端口的浏览器，"
            f"之后本命令会记住它的路径"
        )
    if not profile:
        return False, "这个 Chrome 没有 --user-data-dir（用的是默认 profile）：建议改用专用 profile 再自动化"

    if victim is None:
        detail_prefix = "浏览器原本没在跑，已按上次配置拉起"
    else:
        detail_prefix = "浏览器已重启"

    if dry_run:
        action = f"关闭 1 个 Chrome 主进程并重开" if victim is not None else "直接拉起（当前没在跑）"
        return True, f"[演练] 将{action}：{profile} @ {port}"

    if victim is not None:
        try:
            victim.terminate()
            victim.wait(timeout=15)
        except Exception:  # noqa: BLE001 - 关不掉就让下面的端口轮询判失败
            pass

    import subprocess  # noqa: PLC0415

    subprocess.Popen(  # noqa: S603 - 参数全部来自本机进程命令行/缓存，无 shell
        [
            exe,
            f"--remote-debugging-port={port}",
            f"--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "https://www.zhipin.com/web/geek/jobs",
        ],
        close_fds=True,
    )

    import time  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as rsp:
                if rsp.status == 200:
                    _save_launch(cache, exe, profile, port)
                    return True, f"{detail_prefix}（{port} 端口可用，profile={profile}）"
        except Exception:  # noqa: BLE001 - 还没起来，继续等
            time.sleep(1.5)
    return False, f"浏览器进程已启动，但 {port} 端口 40 秒内没起来：请手动确认窗口是否正常"


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="风控恢复：清锁 → 单页探测 → 归档熔断（不启动发送）")
    parser.add_argument(
        "--confirmed",
        action="store_true",
        help="确认已在官方页面手动验证过（能翻页、能点立即沟通）。不加这个参数只体检，不动文件",
    )
    parser.add_argument("--dry-run", action="store_true", help="演练：打印将做什么，不落手")
    parser.add_argument(
        "--restart-browser",
        action="store_true",
        help="顺手重启专用 Chrome（code 37 的标准动作；需要 JOBPILOT_CHROME_EXE / JOBPILOT_CHROME_PROFILE_DIR）",
    )
    parser.add_argument("--page", type=int, default=1, help="探测用第几页（默认 1，别调大）")
    parser.add_argument(
        "--probe-runs", type=int, default=1, help="同一进程连做几次单页探测（默认 1；实验 A 用 2）"
    )
    parser.add_argument("--probe-gap", type=int, default=90, help="探测间隔秒数（默认 90，别调小）")
    parser.add_argument("--db", default="data/jobpilot-new.db", help="sqlite 库路径（定位熔断文件）")
    parser.add_argument(
        "--force",
        action="store_true",
        help="无视当日探测配额强跑（会记进 probe_budget.json，让冒险变成有意识的动作）",
    )
    args = parser.parse_args(argv)

    from app.config import load_config  # 延迟 import：--help 不依赖项目配置

    cfg = load_config()
    if str(cfg.get("adapter") or "") != "boss":
        print(f"当前 adapter={cfg.get('adapter')!r}，不是 boss：这套恢复只对真机有意义")
        return 3

    db_path = Path(args.db)
    info = inspect(cfg, db_path)
    print(render(info))
    print()
    print(render_environment(check_environment()))
    print()

    if not args.confirmed:
        print("只做了体检。确认平台已恢复后加 --confirmed 再跑：")
        print("  1) 正常浏览器打开职位列表页，能翻页、能点一次【立即沟通】")
        print("  2) python -m app.tools.recover --confirmed")
        return 4

    locks = [Path(item["path"]) for item in info["locks"]]
    if args.restart_browser:
        ok, detail = restart_browser(cfg, dry_run=args.dry_run)
        print(f"\n重启浏览器：{'完成' if ok else '未完成'} —— {detail}")
        if not ok:
            print("（不自动降级成'就这么跑'：环境没换新，探测结果不可信）")
            return 3

    removed = clear_locks(locks, dry_run=args.dry_run)
    print(("将清除" if args.dry_run else "已清除") + f" {len(removed)} 处上游锁")
    for path in removed:
        print(f"  - {path}")

    if args.dry_run:
        # 演练不探测：锁还在，探测必然被本地拒，打一次真请求只会让人误判成"平台还在拒"。
        print("\n演练模式：跳过探测与归档（探测必须建立在**真的清过锁**之上）。")
        return 0

    # 【2026-11-13】每日探测配额：今天已经试过 N 次就别再试。
    #   我们撞的 5 次风控里至少有 3 次是"诊断性请求"自己敲出来的，
    #   靠自觉不靠谱，做成会失败的硬限制。
    from app.core import probe_budget

    limit = int((cfg.get("safety") or {}).get("probe_quota_per_day") or probe_budget.DEFAULT_LIMIT)
    allowed, budget_state = probe_budget.consume("recover_probe", limit=limit)
    if not allowed and not args.force:
        print(f"\n今日探测配额已用完：{probe_budget.describe(budget_state)}")
        print("  这不是故障，是保护：反复探测只会把「刚被拒」变成「反复被拒」。")
        print("  真要强跑加 --force（会被记录）。")
        return 5
    if not allowed:
        print(f"\n（--force）无视配额：{probe_budget.describe(budget_state)}")
    else:
        print(f"\n{probe_budget.describe(budget_state)}")

    ok, results, elapsed = probe_runs(
        cfg, page=args.page, runs=max(args.probe_runs, 1), gap_s=max(args.probe_gap, 0)
    )
    print(f"\n单页探测（page={args.page}，同进程 {max(args.probe_runs, 1)} 次）："
          f"{'全部通过' if ok else '未通过'}")
    for line in results:
        print(f"  {line}")
    if not ok:
        blob = " ".join(results)
        if "BROWSER_SESSION_NOT_FOUND" in blob or "AUTH" in blob:
            # 这不是风控：刚重启的浏览器还没登录。给对的动作，别让人白等一天。
            print("\n这不是风控，是浏览器会话没起来：在刚弹出的窗口里确认已登录")
            print("（必要时扫码），然后**重跑这一条命令**即可。")
        else:
            print("\n今天到此为止：不要再点开始。等平台侧恢复后重新走一遍（先手动确认）。")
            print("如果刚清完锁就被拒，按社区口径**冷却 30–60 分钟**再来一次，别连着试。")
        return 2

    archived = archive_state(info["state_path"], dry_run=args.dry_run)
    if archived:
        print(f"{'将归档' if args.dry_run else '已归档'}熔断文件 → {archived}")
    else:
        print("没有熔断文件需要归档")

    print("\n下一步（脚本不替你做）：")
    print("  1) 重启服务（改过 Python/前端才需要；仅恢复风控时也要重启，进程里可能留着 fail-closed 标记）")
    print("     .\\.venv2\\Scripts\\python.exe -m app.tools.launch --detach --no-start")
    print("  2) 【必须等 10-15 分钟】再起跑。探测是一次搜索，起跑是连续搜索；")
    print("     2026-11-15 现场：17:11 探测放行（231s），17:12 起跑，17:14 第一轮就被 ENVIRONMENT_RISK 拒掉。")
    print("     .\\.venv2\\Scripts\\python.exe -m app.tools.launch --detach --cap 20 --delay-min 12")
    print("     （--delay-min 会真的等，命令行停在那儿是正常的）")
    print("  3) 3-5 分钟后看 /api/pacing/status 的 inflight；inflight=false 才有资格看 last_verdict")
    print("  4) 探测耗时也是信号：正常单页约 105s，超过 200s 说明环境仍在边缘，当天只跑小批量")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
