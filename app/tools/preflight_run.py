"""开跑前体检：一条命令回答「今天能不能起跑」。

## 为什么需要它

2026-11-14 早上真实踩过两次坑，都是**人在起跑前那一刻判断失误**：

1. 我把 09:04 误看成 09:25，以为 `inflight=True` 卡了 20 分钟，
   连 stop 两次、kill 两次进程 —— 实际上那一轮才跑了 3 分钟，
   search 本身就要 105 秒，四条发送要 11 分钟。**过度干预比不管更糟**：
   每次强杀都在丢弃已经发出的请求。
2. 反过来，昨天（11-13 09:00）真的出现过「recover 探测失败、锁被写回、
   `multi start` 照样点开始」—— 那时缺的是**起跑前的一道闸**。

所以这个工具做两件事：
- 把「今天能不能跑」的每一项证据摊开，人不用凭记忆判断；
- 全绿才算通过，任何一项红就打印**具体该怎么办**，而不是让人猜。

## 它不做什么

- **不碰平台**：不搜索、不打招呼、不探测风控。只读本地文件 + 查本机 CDP + 查出口 IP。
- **不改配置**：目标、节奏、账号一律不动（那是人的决定）。
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 上一次体检记下的环境指纹，用来判断「环境变了没有」。
#: 变化本身不是错，但**必须知道变了** —— 11-13 那天同时换了 IP 和端口，
#: 结果无法归因到底是哪一项让风控消失的。
ENV_SNAPSHOT = PROJECT_ROOT / "data" / "last_env.json"

OK = "✅"
BAD = "❌"
WARN = "⚠️ "


def _get_json(url: str, timeout: float = 6.0) -> Any:
    # 本地 CDP 必须绕过系统代理（Clash TUN 会把 127.0.0.1 之外的一切接管）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _tcp_ok(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_service(port: int) -> tuple[bool, str]:
    try:
        data = _get_json(f"http://127.0.0.1:{port}/api/pacing/status")
    except Exception as exc:  # noqa: BLE001
        return False, f"服务 {port} 不可达（{type(exc).__name__}）→ 先 launch"
    used, target = data.get("used"), data.get("target")
    inflight = data.get("inflight")
    started = data.get("pass_started_at")
    extra = ""
    if inflight and started:
        # 关键提示：batch=4 之后一轮要 10–13 分钟，不要用「几分钱没动」的直觉判断卡死
        try:
            began = datetime.fromisoformat(str(started))
            minutes = (datetime.now() - began).total_seconds() / 60
            extra = f"，本轮已跑 {minutes:.0f} 分钟"
            if minutes > 30:
                return False, f"本轮已跑 {minutes:.0f} 分钟仍未返回 → 可能是真卡死，重启进程{extra}"
        except ValueError:
            pass
    return True, f"running={data.get('running')} used={used}/{target} inflight={inflight}{extra}"


def check_browser(cdp_port: int) -> tuple[bool, str]:
    if not _tcp_ok("127.0.0.1", cdp_port):
        return False, f"调试端口 {cdp_port} 未监听 → 先启动专用浏览器"
    try:
        pages = _get_json(f"http://127.0.0.1:{cdp_port}/json/list")
    except Exception as exc:  # noqa: BLE001
        return False, f"CDP 无响应（{type(exc).__name__}）"
    urls = [str(p.get("url") or "") for p in pages if p.get("type") == "page"]
    logged_in = any("zhipin.com" in u and "login" not in u for u in urls)
    if not logged_in:
        return False, f"没看到已登录的 BOSS 页面（当前 {len(urls)} 个标签）→ 扫码登录"
    return True, f"已登录（{len(urls)} 个标签）"


def check_locks(account: dict) -> tuple[bool, str]:
    hits = []
    for key in ("data_dir", "budget_dir"):
        value = account.get(key)
        if value and (Path(value) / "cdp_risk_lock.json").exists():
            hits.append(str(value))
    if hits:
        return False, f"风控锁仍在（{len(hits)} 处）→ 先 recover，再冷却 30–60 分钟"
    return True, "无锁"


def check_supply(db_path: Path, target: int) -> tuple[bool, str]:
    import sqlite3

    if not db_path.exists():
        return False, f"库不存在：{db_path}"
    conn = sqlite3.connect(str(db_path))
    try:
        total = conn.execute("select count(*) from jobs").fetchone()[0]
        left = conn.execute(
            "select count(*) from jobs where job_id not in "
            "(select target from actions where action_type='greet')"
        ).fetchone()[0]
    finally:
        conn.close()
    # 供给判定：够今天用吗？按「目标 + 20% 余量」算，不够就提前说，
    # 否则跑到一半没岗位、反复空搜，那才是风控的来源。
    need = int(target * 1.2)
    if left < target:
        return False, f"未打岗位 {left} 个 < 今日目标 {target} → 会中途缺岗位，先加城市/关键词"
    if left < need:
        return WARN.strip(), f"未打岗位 {left} 个，够 {target} 但离 {need}（+20% 余量）偏紧"
    return True, f"未打 {left} 个 / 库内共 {total} 个（今日目标 {target}）"


def check_env(record: bool) -> tuple[bool, str]:
    """出口 IP：变了不算错，但必须让人知道。"""
    ip = ""
    for url in ("http://ip.3322.net/", "https://myip.ipip.net/json"):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
            import re

            found = re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", body)
            if found:
                ip = found[0]
                break
        except Exception:  # noqa: BLE001
            continue
    if not ip:
        return WARN.strip(), "出口 IP 查不到（网络或代理问题）→ 不影响本地起跑，但无法对比环境"

    previous = {}
    if ENV_SNAPSHOT.exists():
        try:
            previous = json.loads(ENV_SNAPSHOT.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            previous = {}

    old_ip = previous.get("ip")
    if record:
        ENV_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        ENV_SNAPSHOT.write_text(
            json.dumps(
                {"ip": ip, "at": datetime.now().isoformat(timespec="seconds")},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    if not old_ip:
        return True, f"{ip}（首次记录，明天起可对比）"
    if old_ip != ip:
        return WARN.strip(), f"变了：{old_ip} → {ip}（换 IP 是好牌，但**一天只动一个变量**）"
    return True, f"{ip}（与上次一致）"


def check_config(cfg: dict) -> tuple[bool, str]:
    from app.core import pacing as pacing_mod

    # 【2026-11-16】从已废弃的 start-jobpilot.ps1 搬过来的两条防呆。
    # 那个脚本的注释写着"这个坑踩过 4 次了"——config.yaml 被覆盖回默认值时，
    # adapter 会变回 fake（跑假数据、看着正常其实什么都没发），
    # target.search_url 会变空（走 --city 触发 INVALID_PARAM）。
    # 这两条必须在起跑前拦住，否则"跑了一天"其实全是假的。
    if cfg.get("adapter") != "boss":
        return False, f"adapter={cfg.get('adapter')!r}，不是 boss → 会跑假数据，先修 config.yaml"
    if not (cfg.get("target") or {}).get("search_url"):
        return False, "target.search_url 为空 → 会触发 INVALID_PARAM，先补上搜索 URL"

    pcfg = cfg.get("pacing") or {}
    quota = cfg.get("quota") or {}
    cap = int(quota.get("daily_greet_max") or 0)
    try:
        today_target = pacing_mod.ramp_target(
            pacing_mod.parse_config(pcfg, daily_cap=cap), date.today(), daily_cap=cap
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"pacing 配置无法解析：{exc}"
    batch = pcfg.get("send_batch", 1)
    round_size = pcfg.get("round")
    detail = (
        f"今日目标 {today_target} 条 · send_batch={batch} · round={round_size} · "
        f"间隔 {pcfg.get('interval_s')}s · 轮间 {pcfg.get('round_gap_s')}s"
    )
    if batch and round_size and isinstance(round_size, (list, tuple)) and batch > round_size[1]:
        return WARN.strip(), detail + " ← send_batch 大于 round 上限，轮次会抖动"
    return True, detail


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    record = "--record" in args

    sys.path.insert(0, str(PROJECT_ROOT))
    from app.config import load_config
    from app.core import accounts as accounts_mod

    cfg = load_config()
    account = accounts_mod.active_account(cfg) or {}
    port = int(account.get("service_port") or 8010)
    cdp_port = int(account.get("cdp_port") or 9222)
    db_path = PROJECT_ROOT / str(account.get("db") or "data/jobpilot-new.db")
    target = 0
    try:
        from app.core import pacing as pacing_mod

        quota = cfg.get("quota") or {}
        cap = int(quota.get("daily_greet_max") or 50)
        target = pacing_mod.ramp_target(
            pacing_mod.parse_config(cfg.get("pacing") or {}, daily_cap=cap),
            date.today(),
            daily_cap=cap,
        )
    except Exception:  # noqa: BLE001
        pass

    print(f"账号 {account.get('id', '?')} · {account.get('name', '默认')}")
    print("-" * 62)

    checks: list[tuple[str, bool, str]] = []
    name, ok, detail = "服务", *check_service(port)
    checks.append(("服务", ok, detail))
    checks.append(("浏览器", *check_browser(cdp_port)))
    checks.append(("风控锁", *check_locks(account)))
    checks.append(("供给", *check_supply(db_path, target or 50)))
    checks.append(("出口 IP", *check_env(record)))
    checks.append(("今日配置", *check_config(cfg)))

    failed = 0
    warned = 0
    for label, ok, detail in checks:
        mark = OK if ok is True else (BAD if ok is False else WARN.strip())
        if ok is False:
            failed += 1
        elif ok is not True:
            warned += 1
        print(f"{mark} {label:<6} {detail}")

    print("-" * 62)
    if failed:
        print(f"{BAD} {failed} 项未通过：按上面的提示逐项处理，**不要直接起跑**。")
        return 1
    if warned:
        print(f"{WARN.strip()} 全部可用，但有 {warned} 项提醒（看完再决定）。")
        return 0
    print(f"{OK} 全绿，可以起跑：python -m app.tools.launch --detach --cap {target or 50}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
