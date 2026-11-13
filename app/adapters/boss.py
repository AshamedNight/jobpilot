"""BossAdapter —— 真适配器（任务书 Phase2 §3 + 补丁 P2-A v0.4.1 + P2-B §7）。

本批的写通路**只开一条**：`greet`（单发）。`exchange` / `batch_greet` 仍抛
`NotImplementedError`——`exchange` 归 P2-D；`batch_greet` 是**用不了**而不是没做
（上游 `batch-greet` 只有 `QUERY + --city`，没有 `--url`，淄博不在 `CITY_CODES` 里，
必返 `INVALID_PARAM`，见 §8 + 补页 §B）。这条闸门是**防止误发**，不是待补的 TODO。

补丁 v0.4.1 的 6 条硬要求（每条都有实测依据，写在对应代码旁）：

1. §1.1 `standalone_mode=True` + `prog_name="boss"` + 显式 `--json`。
   `False` 时用法错误（`NoSuchCommand` / `NoSuchOption`）**根本不发信封**、stdout 是空的，
   等于丢掉上游错误码。`prog_name` 不传则信封 `command` 字段取 `sys.argv[0]`
   （pytest 下变 `t1.py`、uvicorn 下变 `uvicorn`）。
2. §1.8 模块级 `threading.Lock` 包住 `redirect_stdout + cli()`。
   `redirect_stdout` 改的是**进程全局** `sys.stdout`，非线程安全；实测 16 线程并发
   有 6/16 拿到空串 → 被当成 `UPSTREAM_ERROR`，表现为"随机少几条"。用 `cli(args=...)`
   传参，**不改 `sys.argv`**（同样是全局竞态，且更难发现）。
3. §1.2 `search` / `chat` / `chatmsg` 的 `data` 确定是 list。兼容 dict 保留，
   但**不许静默兜底**——形状异常要显式报 `UPSTREAM_ERROR`。静默兜底会让每条 job
   变成 `{"job_id": None}`，全被 `skipped_no_id` 丢弃，最后 `found=0` 而无人知道是形状错了。
4. §1.4 `chatmsg` 一并实现（`dashboard.py:272` 的 followup 硬依赖它）。
   上游没有 `platform_mid`，`from` 是**名字字符串**不是 in/out。
5. §1.5 `welfare` 是 list、`raw_json` 是 dict，而两列都是 TEXT → 落库前必须 `json.dumps`。
   否则 sqlite 抛 `ProgrammingError: type 'list' is not supported`。
6. §1.7 `hr_active` 必须映射：上游 `boss_active` 是中文「在线/离线」，
   `boss_active_desc` 才是「刚刚活跃」这类文案；`half-year` → `half_year`。

三条不可违反的约束（任务书 §1 / 探针报告 §3）：

- **不许依赖 PATH 里的 `boss` / `boss.exe`**（探针发现 ③：被执行策略拦截）。
  只用进程内 `from boss_agent_cli.main import cli`；**不用** `subprocess`、**不用** `shell`。
- **不许碰 `~/.boss-agent/`**（主号登录态）。`data_dir` 默认 `~/.boss-agent-test/`，
  自己算绝对路径显式传 `--data-dir`（全局选项，必须在子命令**前面**；上游无环境变量兜底）。
- **`security_id` 只落库，不出现于任何返回值**。`chat()` 因此不透传上游 `security_id`——
  `uid` 才是跨请求稳定的主键。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import threading
import time
from pathlib import Path
from typing import Any

from app.adapters.base import err_result, ok_result

# --------------------------------------------------------------------------- #
# 上游常量（来自 `boss schema` 3.0.1，见 probe-raw汇总.md，不是记忆）
# --------------------------------------------------------------------------- #
#: 期望的上游版本。不符只告警，不改行为（探针 G-5）。
EXPECTED_UPSTREAM_VERSION = "3.0.1"

#: 本批暴露的读能力。
READ_CAPABILITIES: frozenset[str] = frozenset({"search", "detail", "chat"})

#: 【P2-B §9③】本批新开的写能力：只有单发 `greet`。
WRITE_CAPABILITIES: frozenset[str] = frozenset({"greet"})

#: `probe()` 赋给 `capabilities` 的最终集合。
#: **由常量决定，不去解析 `schema` 输出猜能力**（§7.2 末条）：schema 是上游 CLI 的
#: 自描述，字段随版本变，用它推断"我能做什么"等于把能力开关交给上游措辞。
#: `exchange` 刻意**不在**这里——P2-D 之前它一直是闭的（E2 断言守着）。
CAPABILITIES: frozenset[str] = frozenset(READ_CAPABILITIES | WRITE_CAPABILITIES)

# 56 个 error_codes 里**只有这几组需要特判**（Step0 §3.1），其余一律 UPSTREAM_ERROR。
#: 已经打过 = 我们要的结果，归 `OK`（幂等成功）。
#: **归成错误会让重跑 sweep 把"上次已成功"记成失败，actions 里出现假失败。**
ERROR_CODES_ALREADY_OK: frozenset[str] = frozenset({"ALREADY_GREETED", "ALREADY_APPLIED"})
#: 发了但没确认 → 永不重试（§1 第 10 条）。
#: 【闭环 1 · 2026-11-04】`TIMEOUT` 归进来：超时的意思是"可能发出去了也可能没发"，
#: 与 UNCONFIRMED 完全同义。归成 `UPSTREAM_ERROR` 会让 `greet` 记成 failed 并继续下一条，
#: 而那条招呼可能已经真的发出去了 —— 那是最不该出现的一类重复发送。
ERROR_CODES_UNCONFIRMED: frozenset[str] = frozenset(
    {"GREET_RESULT_UNKNOWN", "ACTION_UNCONFIRMED", "RESUME_ACCEPT_RESULT_UNKNOWN", "TIMEOUT"}
)
#: 命中即全停，`/api/pause` 清不掉。
ERROR_CODES_RISK: frozenset[str] = frozenset(
    {"ACCOUNT_RISK", "ENVIRONMENT_RISK", "ENVIRONMENT_RISK_LOCKED", "LOGIN_RISK_CONTROL"}
)
#: 命中即停批，不自动重试。
ERROR_CODES_LIMIT: frozenset[str] = frozenset(
    {"GREET_LIMIT", "RATE_LIMITED", "REQUEST_BUDGET_EXHAUSTED"}
)
#: 提示用户 `boss login`。
ERROR_CODES_AUTH: frozenset[str] = frozenset(
    {"AUTH_REQUIRED", "AUTH_EXPIRED", "LOGIN_EXPIRED", "TOKEN_REFRESH_FAILED"}
)
#: 岗位下架，跳过。
ERROR_CODES_GONE: frozenset[str] = frozenset({"JOB_NOT_FOUND"})

#: `--active` 的合法取值（上游 `ACTIVE_LEVELS` 的键，注意 `half-year` 是连字符）。
ACTIVE_LEVELS: tuple[str, ...] = ("online", "today", "3d", "week", "2w", "month", "half-year")

#: 上游 `active_desc_rank()` 返回 rank 数字，我们要反查成 `models.jobs.hr_active` 的枚举键。
#: `half-year`(上游) → `half_year`(我们)。认不出的 rank → `unknown`，不许猜。
_RANK_TO_KEY: dict[int, str] = {
    7: "online",
    6: "today",
    5: "3d",
    4: "week",
    3: "2w",
    2: "month",
    1: "half_year",
}

#: `active_desc_rank` 不可用时的本地兜底（照抄上游 9 种文案，见补丁 §1.7 实测）。
_ACTIVE_DESC_LOCAL: dict[str, int] = {
    "在线": 7,
    "刚刚活跃": 7,
    "今日活跃": 6,
    "3日内活跃": 5,
    "本周活跃": 4,
    "2周内活跃": 3,
    "本月活跃": 2,
    "近半年活跃": 1,
    "半年前活跃": 0,
}

#: `--page` 最多翻几页（上游无 `--limit`，结果上限由它自己的 RESULT_LIMIT_REACHED 管）。
DEFAULT_PAGES = 1

#: stderr 截断长度（`detail` 字段用，避免把整段日志灌进库里）。
_STDERR_LIMIT = 500

#: 【补丁 §1.8】模块级锁，**不是实例级**：`redirect_stdout` 动的是进程全局 `sys.stdout`。
_CLI_LOCK = threading.Lock()

#: 【闭环 1 · 2026-11-04 事故】某次 CLI 调用超时后进入"暂停调用上游"状态。
#:
#: 为什么要 fail-closed：卡住的那次调用仍在另一个线程里跑，它同时占着 `_CLI_LOCK`
#: 和**进程全局**的 `sys.stdout`（`redirect_stdout` 还没退出）。这时候再发一次，
#: 只会排队排到超时，把"上游卡死"伪装成"上游很慢"。
#:
#: 【2026-11-06 事故】原来这个标记是**永久**的，只有重启服务能清。结果：
#: 09:19 一次瞬时卡顿（300 秒）把进程永久锁死，而实际上游几分钟后就恢复了 ——
#: 剩下 8 个半小时全在"立即失败 + 冷却 300 秒"里空转，当天只发出 1 条。
#: 现在改成**限时**：到期自动允许重试（见 `_STUCK_RETRY_SECONDS`），
#: 既保留"卡住时不自欺"的语义，又不让一次卡顿毁掉整天。
_CLI_STUCK_UNTIL: float | None = None

#: 超时后暂停多久再允许重试上游（秒）。比 `unconfirmed` 冷却略长，
#: 避免"刚冷却完又立刻撞同一个卡点"，同时保证一天里能自愈多次。
_STUCK_RETRY_SECONDS = 10 * 60

#: 超时后 `last_error` 里的 detail（页面/日志靠它区分"平台拒了"与"我们等了太久"）。
_STUCK_DETAIL = (
    "上游 CLI 调用超时：暂停调用上游，到期自动重试；重启服务可立即恢复"
)


def reset_cli_state() -> None:
    """清掉"暂停调用"标记（测试用；生产环境到期会自动恢复，不必重启）。"""
    global _CLI_STUCK_UNTIL
    _CLI_STUCK_UNTIL = None


def cli_stuck() -> bool:
    """当前是否处于"暂停调用上游"状态（排障/测试用）。"""
    return _CLI_STUCK_UNTIL is not None and time.time() < _CLI_STUCK_UNTIL


def stuck_until() -> float | None:
    """暂停调用到什么时候（epoch 秒），没暂停则 None。"""
    return _CLI_STUCK_UNTIL if cli_stuck() else None


def _cli_entry() -> Any:
    """返回上游 click 入口。

    单独抽成模块级函数是**测试缝合点**：测试 monkeypatch 它即可验 argv 形状，
    不必真装 `boss-agent-cli`（它拖着 patchright 一大串依赖）。
    生产路径就是 `from boss_agent_cli.main import cli`。
    """
    from boss_agent_cli.main import cli  # noqa: PLC0415 - 延迟 import 是刻意的

    return cli


def normalize_error_code(code: str | None) -> str | None:
    """上游 `error.code` → 我们的归一码。

    `ALREADY_GREETED` / `ALREADY_APPLIED` → `OK`（**幂等成功，不是错误**）。
    其余按 Step0 §3.1 的 6 组特判，剩下的归 `UPSTREAM_ERROR`。
    """
    if not code:
        return "UPSTREAM_ERROR"
    text = str(code).strip().upper()
    if text in ERROR_CODES_ALREADY_OK:
        return "OK"
    if text in ERROR_CODES_UNCONFIRMED:
        return "UNCONFIRMED"
    if text in ERROR_CODES_RISK:
        return "RISK_LOCKED"
    if text in ERROR_CODES_LIMIT:
        return "LIMIT"
    if text in ERROR_CODES_AUTH:
        return "AUTH"
    if text in ERROR_CODES_GONE:
        return "GONE"
    return "UPSTREAM_ERROR"


def _active_rank(desc: Any) -> int | None:
    """上游文案 → rank。优先用上游自己的实现，包没装时退回本地表。"""
    text = str(desc or "").strip()
    if not text:
        return None
    try:
        from boss_agent_cli.search_filters import active_desc_rank  # noqa: PLC0415

        return active_desc_rank(text)
    except Exception:  # 上游未安装 / 接口改名 → 本地兜底，不抛
        return _ACTIVE_DESC_LOCAL.get(text)


def normalize_active(desc: Any, online_flag: Any = None) -> str:
    """`boss_active_desc` + `boss_active` → `models.jobs.hr_active` 的 9 档枚举。

    上游 `boss_active` 是中文「在线」/「离线」（**不是等级**），
    `boss_active_desc` 才是「刚刚活跃」这类文案（补丁 §1.7）。
    """
    key = _RANK_TO_KEY.get(_active_rank(desc) or -1)
    if key:
        return key
    # 列表明确在线时 active_desc 可能为空，但 bossOnline / boss_active 给出「在线」
    if str(online_flag or "").strip() == "在线":
        return "online"
    return "unknown"


def _serialize(value: Any) -> Any:
    """【补丁 §1.5】`welfare`(list) / `raw_json`(dict) 落 TEXT 列前必须序列化。

    字符串原样传（保持 fake 兼容）。不做这一层，sqlite 会抛
    `ProgrammingError: Error binding parameter N: type 'list' is not supported`。
    """
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


class BossAdapter:
    """真 BOSS 适配器（只读）。形状对齐 `app.adapters.base.Adapter`。"""

    capabilities: set[str]

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        options = (cfg or {}).get("adapter_options") or {}
        self.timeout_s = int(options.get("timeout_s") or 60)
        #: 自己算绝对路径；**绝不**落到默认的 `~/.boss-agent`（主号登录态）。
        #: 上游不校验绝对路径（传相对路径会按 CWD 解析）→「绝对路径」是我们的责任。
        self.data_dir = (
            Path(str(options.get("data_dir") or "~/.boss-agent-test/")).expanduser().resolve()
        )
        self.active = str(options.get("active") or "online").strip().lower()
        if self.active not in ACTIVE_LEVELS:
            self.active = "online"  # 默认 online：只看列表、不额外查详情（Step0 §4）
        self.pages = max(int(options.get("pages") or DEFAULT_PAGES), 1)
        #: 真机 search 必须走 `--url`（补丁 §2：淄博不在上游 40 城表里）。
        #: 从 `target.search_url` 读默认值——**不改 dashboard 的 sweep 调用**（补丁 §6），
        #: 由适配器自己兜住，这样 `config.yaml` 一填就生效。
        self.search_url = str((cfg or {}).get("search_url") or "").strip()

        self.capabilities = set()
        #: 最近一次失败的 `err_result(...)`（测试与排障用）。
        self.last_error: dict[str, Any] | None = None
        #: 本批丢弃计数，只记数不上报敏感值。
        self.stats: dict[str, int] = {}

    # -- 版本 ------------------------------------------------------------ #
    @staticmethod
    def upstream_version() -> str | None:
        """上游实际版本。没装就返回 None（不改行为，只给 `/api/status` 用）。"""
        try:
            from boss_agent_cli import __version__  # noqa: PLC0415

            return str(__version__)
        except Exception:  # pragma: no cover - 没装上游包时
            return None

    # -- CLI 调用层：唯一碰上游的地方 ------------------------------------ #
    def _build_args(self, *args: str) -> list[str]:
        """全局选项必须写在子命令**前面**（Step0 §4.1），`--json` 紧随其后（补丁 §1.1）。"""
        return ["--data-dir", str(self.data_dir), "--json", *args]

    def _run_cli(self, *args: str) -> dict[str, Any]:
        """调一次上游 CLI，返回**信封**。任何失败都不抛异常，只写 `last_error`。

        【闭环 1 · 2026-11-04 事故】`adapter_options.timeout_s` 原来只赋值不使用，
        于是"上游一发请求卡住"= 这个进程永远等下去（真机实测：一轮 pass 卡了 10 分 24 秒，
        期间页面 `last_verdict` 一直是 null，看起来像"引擎没跑"）。

        现在：调用跑在独立守护线程里，主线程 `join(timeout_s)` 到点就返回 `TIMEOUT`，
        并让"暂停调用上游"状态生效 **`_STUCK_RETRY_SECONDS` 秒**（不是永久）——
        见 `_CLI_STUCK_UNTIL` 上的两次事故记录。
        """
        global _CLI_STUCK_UNTIL

        self.last_error = None
        argv = self._build_args(*args)

        if _CLI_STUCK_UNTIL is not None:
            if time.time() < _CLI_STUCK_UNTIL:
                return self._fail("TIMEOUT", _STUCK_DETAIL)
            # 暂停期已过：允许重试。上游早就恢复了，不该因为一次瞬时卡顿锁死整天。
            _CLI_STUCK_UNTIL = None

        buffer = io.StringIO()

        # 【补丁 §1.1】进程内 import；包已钉在 requirements.txt，不走 subprocess
        # （回退分支是死代码，还多一个 PATH / shell 风险面）。
        cli = _cli_entry()

        box: dict[str, Any] = {}

        def _call() -> None:
            # 【补丁 §1.8】整段（redirect + cli）必须被模块级锁包住。
            # 取锁也要有上限：上一轮卡死的调用可能还握着它。
            if not _CLI_LOCK.acquire(timeout=self.timeout_s):
                box["lock_timeout"] = True
                return
            try:
                with contextlib.redirect_stdout(buffer):
                    try:
                        cli(
                            args=argv,
                            prog_name="boss",  # 不传则信封 command 取 sys.argv[0]
                            standalone_mode=True,  # False 会让用法错误吞掉信封
                        )
                    except SystemExit:
                        # 业务错误也是 SystemExit(1) + 完整信封 → 继续解 stdout
                        pass
                    except Exception as exc:  # click.NoSuchCommand 等非 SystemExit
                        box["exc"] = exc
            finally:
                _CLI_LOCK.release()

        worker = threading.Thread(target=_call, name="boss-cli", daemon=True)
        worker.start()
        worker.join(self.timeout_s)

        if worker.is_alive():
            _CLI_STUCK_UNTIL = time.time() + _STUCK_RETRY_SECONDS
            command = args[0] if args else "?"
            return self._fail(
                "TIMEOUT",
                f"上游 CLI `{command}` 超过 {self.timeout_s}s 未返回，"
                f"暂停调用上游 {_STUCK_RETRY_SECONDS // 60} 分钟（到期自动重试）。{_STUCK_DETAIL}",
            )
        if box.get("lock_timeout"):
            # 锁被上一轮卡死的调用占着：同样按"暂停"处理，但**不算**上游超时，
            # 所以不延长暂停窗口 —— 否则两个计时器会互相续命。
            return self._fail(
                "TIMEOUT",
                f"等待上游 CLI 锁超过 {self.timeout_s}s（上一轮调用仍未退出）：{_STUCK_DETAIL}",
            )
        exc = box.get("exc")
        if exc is not None:
            return self._fail("UPSTREAM_ERROR", f"{type(exc).__name__}: {exc}")

        return self._parse_envelope(buffer.getvalue())

    def _parse_envelope(self, stdout: str) -> dict[str, Any]:
        """stdout 只有 JSON（`conventions.stdout`），日志走 stderr。"""
        text = (stdout or "").strip()
        if not text:
            return self._fail("UPSTREAM_ERROR", "上游无输出（stdout 为空）")
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            return self._fail("UPSTREAM_ERROR", text[:_STDERR_LIMIT])
        if not isinstance(envelope, dict):
            return self._fail("UPSTREAM_ERROR", text[:_STDERR_LIMIT])
        if envelope.get("ok") is False:
            error = envelope.get("error") or {}
            code = normalize_error_code(error.get("code"))
            self.last_error = err_result(code, detail=str(error.get("message") or "")[:200])
            if code == "OK":
                # 幂等成功要对调用方体现成成功，不要把"已打过招呼"当失败
                envelope = dict(envelope, ok=True, data=envelope.get("data"))
        return envelope

    def _fail(self, code: str, detail: str | None) -> dict[str, Any]:
        """记一条失败并返回 `ok=False` 的空信封（**不抛异常**）。"""
        self.last_error = err_result(code, detail=detail)
        return {
            "ok": False,
            "data": None,
            "error": {"code": code, "message": detail},
            "hints": None,
        }

    def _record_upstream_failure(self, command: str, envelope: dict[str, Any]) -> None:
        """【F2】把上游错误码留下来，供 dashboard 上报。

        `found=0` 至少有四种完全不同的原因（没登录 / 参数非法 / 被风控 / 真的没岗位），
        不记下来用户只能看到一个 0。**只记 code 与 message，不记原始信封**
        （防 `security_id` / `raw_json` 之类的敏感字段外泄）。
        """
        error = envelope.get("error") or {}
        code = error.get("code")
        message = error.get("message")
        normalized = normalize_error_code(code)
        # 保留 `err_result` 的原有键（`sent` / `error_code` / `detail` / `raw`），
        # 再叠加 F2 需要的诊断键：现有测试与本批新测试都读同一份结构，不二选一。
        self.last_error = {
            **err_result(normalized, detail=str(message or "")[:200]),
            "command": command,
            "code": code,
            "message": message,
            "normalized": normalized,
        }

    @staticmethod
    def _as_list(envelope: dict[str, Any], command: str) -> list[Any] | None:
        """【补丁 §1.2】`data` 是 list；兼容 dict 但形状异常要显式报错，不许静默兜底。"""
        data = envelope.get("data")
        if isinstance(data, dict):  # 防御：上游仍有变形可能
            for key in ("items", "result", "list"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            return None
        return data

    # -- 探针 ------------------------------------------------------------ #
    def probe(self) -> set[str]:
        """启动时探测能力。

        【补丁 §1.3 坑 1】**只许调 `schema`**（本地目录、不联网、不需登录），
        绝不许调 `search` / `chat` / `detail`——否则"起服务"就等于"发请求"，
        风控预算会被白吃。
        """
        if self.upstream_version() is None:
            self.capabilities = set()
            return set()
        envelope = self._run_cli("schema")
        if not envelope.get("ok"):
            self.capabilities = set()
            return set()
        # 【P2-B §9③】`READ_CAPABILITIES` 并入 `greet` 后赋给 `capabilities`。
        # 仍然是常量决定，**不解析 schema 输出**（见 CAPABILITIES 的注释）。
        self.capabilities = set(CAPABILITIES)
        return set(self.capabilities)

    # -- 读：search ------------------------------------------------------ #
    def search(self, query: str = "", **filters: Any) -> list[dict[str, Any]]:
        """搜岗位，映射成与 `jobs` 列对齐的统一字段。

        上游参数（Step0 §2）：位置参数 `QUERY` / `--city` / `--active` / `--page`，
        另加 `--url`（补丁 §2：淄博不在上游 40 城表里，真机必须走 `--url`）。
        **没有 `--limit`**。`cities` 是列表，取第一座。

        `filters["page"]`：**只翻这一页**（不给就按 `pages` 顺序翻到底）。
        这个参数是给节奏引擎用的 —— 2026-11-04 实测：一个进程里连着翻 5 页，
        第 2 页就吃到 `ENVIRONMENT_RISK`；而单页请求连续两次都正常（77s / 177s）。
        所以 pacer 每次只翻一页、跨 pass 轮转页码，把"连续翻页"这个爬虫特征去掉。
        sweep 不传这个参数，行为不变。
        """
        self.stats = {"found": 0, "skipped_no_id": 0, "skipped_no_publish_time": 0}
        city = filters.get("city") or (filters.get("cities") or [None])[0]
        active = str(filters.get("active") or self.active)
        url = filters.get("url") or self.search_url or None

        only_page = filters.get("page")
        page_list = [max(int(only_page), 1)] if only_page is not None else list(
            range(1, self.pages + 1)
        )

        collected: list[dict[str, Any]] = []
        for page in page_list:
            args = ["search"]
            # 【2026-11-12 修】有 `--url` 时**不能再传位置参数 query**：
            # 上游 `commands/search.py:158` 是 `query = query or parsed_url.query`，
            # **位置参数优先**，URL 里的 query 会被整个丢掉。
            # 现场后果：8 个关键词全在搜"法务"，不同 URL 返回同一批 10 条，
            # 多关键词轮转形同虚设 —— 池子几天就被打成"全是已打过"，进而反复空搜触发风控。
            if query and not url:
                args.append(str(query))
            if url:
                args += ["--url", str(url)]
            # 【F1】有 `--url` 时**不传** `--city`：上游 `CITY_CODES` 只有 40 个城市，
            #   淄博/潍坊都不在表内 → `INVALID_PARAM: 未知城市` → 错误被静默吞成 found=0。
            #   而且城市校验**优先于登录检查**，跟登录态无关；url 里已带 city 参数。
            if city and not url:
                args += ["--city", str(city)]
            if active:
                args += ["--active", active]
            args += ["--page", str(page)]

            envelope = self._run_cli(*args)
            if not envelope.get("ok"):
                self._record_upstream_failure("search", envelope)  # 【F2】不许静默
                break  # 失败就停在这一页，已拿到的照常返回
            items = self._as_list(envelope, "search")
            if items is None:  # 【补丁 §1.2】形状异常必须显式报错
                self._fail(
                    "UPSTREAM_ERROR",
                    f"search: data 形状异常 {type(envelope.get('data')).__name__}",
                )
                break
            for item in items:
                mapped = self._map_job(item)
                if mapped is not None:
                    collected.append(mapped)
            if not (envelope.get("pagination") or {}).get("has_more"):
                break

        self.stats["found"] = len(collected)
        return collected

    def _map_job(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """单条列表项 → 统一字段。**`job_id` 与 `security_id` 缺一即丢**（探针发现 ②）。"""
        if not isinstance(item, dict):
            return None
        job_id = str(item.get("job_id") or "").strip()
        security_id = str(item.get("security_id") or "").strip()
        if not job_id or not security_id:
            self.stats["skipped_no_id"] = self.stats.get("skipped_no_id", 0) + 1
            return None

        # 上游 `JobItem.to_dict()` 没有任何发布时间字段（Step0 §1 / 补丁 Step 0.5 ①）
        # → 只能 None，**不许伪造、不许另找来源编**。
        self.stats["skipped_no_publish_time"] = self.stats.get("skipped_no_publish_time", 0) + 1

        return {
            "job_id": job_id,
            "security_id": security_id,
            "title": item.get("title") or "",
            "company": item.get("company") or "",
            "salary": item.get("salary") or "",
            "city": item.get("city") or "",
            "industry": item.get("industry") or "",
            "hr_name": item.get("boss_name") or "",
            "hr_active": normalize_active(item.get("boss_active_desc"), item.get("boss_active")),
            "company_scale": item.get("scale") or "",
            "publish_time": None,
            # 列表项不带 JD 正文（`description` 只在 detail 里）；不在此额外发请求，
            # 保持 `--active online` 的"零额外请求"口径。回填 JD 是 P2-B 的事。
            "jd_text": "",
            # 【补丁 §1.5】上游 welfare 是 list，jobs.welfare 是 TEXT → 必须序列化
            "welfare": _serialize(item.get("welfare")),
            "raw_json": _serialize(item),
        }

    # -- 读：detail ------------------------------------------------------ #
    def detail(self, security_id: str, job_id: str | None = None) -> dict[str, Any]:
        """职位详情。给 `--job-id` 走上游 httpx 快通道（不启浏览器）。"""
        args = ["detail", str(security_id)]
        if job_id:
            args += ["--job-id", str(job_id)]
        envelope = self._run_cli(*args)
        if not envelope.get("ok"):
            # 【P2-B §9①】失败**必须**走 `_record_upstream_failure`——这是 P2-A 遗留 P3 的收口。
            # 原来这里走 `_fail()`，`last_error` 只有 `{sent,error_code,detail,raw}`，
            # 于是 `/api/status.last_upstream_error` 显示成
            # `{"command": null, "code": null, "message": null, "normalized": "..."}`：
            # 看得到"哪一类错"，看不到上游原始 code 与文案，诊断价值打折（复审 §3）。
            #
            # **三档分类不在这里做**（补页 §C/§E）：`JOB_NOT_FOUND`（岗没了）、
            # `REQUEST_BUDGET_EXHAUSTED`（预算）、`ACCOUNT_RISK`（风控）三种后果完全不同，
            # 适配器只**如实上报**，由编排层决定"算失败继续 / 停 enrich / 锁风险整轮终止"。
            # 分类写死在适配器里，等于让最不了解上下文的一层做决策。
            self._record_upstream_failure("detail", envelope)
            return {"ok": False, "error": self.last_error}
        data = envelope.get("data")
        payload = data if isinstance(data, dict) else {}
        return {
            "ok": True,
            "job_id": str(job_id or ""),
            "jd_text": str(payload.get("description") or ""),
            "address": payload.get("address") or "",
            "hr_active": normalize_active(payload.get("boss_active")),
            "greeted": bool(payload.get("greeted")),
        }

    # -- 读：chat -------------------------------------------------------- #
    def chat(self, **filters: Any) -> list[dict[str, Any]]:
        """会话列表。

        上游输出项（Step0 §2.1，`commands/chat.py`）：
        `name / title / brand_name / initiated_by / last_msg / last_time /
        last_ts / msg_status / uid / security_id / encrypt_job_id / unread`。

        **不伪造 `last_msg_from`**：真机没有这个字段，`initiated_by` 的语义是
        "谁**先发起**"，不是"最后一句谁说的"。捏一个会让 fake 与真机行为分叉。
        入站判断在 `dashboard.py:264` 用 `unread` 兜（Step 8）。
        `security_id` 也不透传（敏感，禁止出现在任何 API 响应里）。
        """
        args = ["chat"]
        if filters.get("from_") in ("boss", "me"):
            args += ["--from", str(filters["from_"])]
        if filters.get("days"):
            args += ["--days", str(int(filters["days"]))]
        args += ["--page", str(int(filters.get("page") or 1))]

        envelope = self._run_cli(*args)
        if not envelope.get("ok"):
            self._record_upstream_failure("chat", envelope)  # 【F2】不许静默
            return []
        items = self._as_list(envelope, "chat")
        if items is None:
            self._fail(
                "UPSTREAM_ERROR", f"chat: data 形状异常 {type(envelope.get('data')).__name__}"
            )
            return []

        conversations: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            uid = str(item.get("uid") or "").strip()
            if not uid:
                continue
            try:
                unread = int(item.get("unread") or 0)
            except (TypeError, ValueError):
                unread = 0
            conversations.append(
                {
                    "uid": uid,
                    "job_id": item.get("encrypt_job_id") or None,
                    "hr_name": item.get("name") or "",
                    "company": item.get("brand_name") or "",
                    "last_msg": item.get("last_msg") or "",
                    "last_time": item.get("last_time"),
                    "last_ts": item.get("last_ts"),
                    # 真机唯一可靠的"有新动静"信号（A6 修正）
                    "unread": unread,
                    "initiated_by": item.get("initiated_by") or "未知",
                    "msg_status": item.get("msg_status") or "未知",
                }
            )
        return conversations

    # -- 读：chatmsg（补丁 §1.4，dashboard followup 硬依赖） -------------- #
    def chatmsg(self, uid: str, raw: bool = False) -> list[dict[str, Any]]:
        """单个会话的消息历史。

        上游：`chatmsg UID --page --count --raw`。入参是 `uid`（**不是 `security_id`**，
        后者每请求轮换）。输出 `data` 是 list，每项 `{from, type, text, time}`，
        `--raw` 追加 `{message_id, type_code, timestamp, body, links, security_id, job_id, raw}`。

        两条映射规则（补丁 §1.4）：
        - **上游没有 `platform_mid`**。优先 `message_id`；缺失时拼
          `(timestamp, 方向, 内容 sha1 前 12 位)` 的稳定值；仍为空则丢弃该条——
          **不编假 mid**（沿用 P2-C F1 的"不许编"原则）。
        - **`from` 是名字字符串**（`"我"` / 联系人名），不是 `in/out`。
          `from == "我"` → `out`，否则 → `in`。**不要**拿名字去比对 `hr_name`（会误判）。
        """
        args = ["chatmsg", str(uid), "--page", "1", "--count", str(self.pages * 20)]
        if raw:
            args.append("--raw")

        envelope = self._run_cli(*args)
        if not envelope.get("ok"):
            self._record_upstream_failure("chatmsg", envelope)  # 【F2】不许静默
            return []
        items = self._as_list(envelope, "chatmsg")
        if items is None:
            self._fail(
                "UPSTREAM_ERROR", f"chatmsg: data 形状异常 {type(envelope.get('data')).__name__}"
            )
            return []

        messages: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = item.get("text") or item.get("body") or ""
            sender = str(item.get("from") or "").strip()
            direction = "out" if sender == "我" else "in"
            timestamp = item.get("timestamp") or item.get("time")

            mid = item.get("message_id") or item.get("msgId") or item.get("id")
            if not mid:
                digest = hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:12]
                mid = f"{timestamp}:{direction}:{digest}"
            if not mid:
                continue  # 拿不到稳定 ID 的消息宁可不存，也不编假 mid

            messages.append(
                {
                    "platform_mid": str(mid),
                    "direction": direction,
                    "msg_type": str(item.get("type") or "text"),
                    "content": str(text),
                    "created_at": str(timestamp) if timestamp else None,
                }
            )
        return messages

    # -- 写操作 ---------------------------------------------------------- #
    def greet(self, security_id: str, job_id: str, message: str) -> dict[str, Any]:
        """真打招呼（**单发**）。命令面与信封见补页 §B。

        ```text
        boss --data-dir <abs> --json greet <SECURITY_ID> <JOB_ID> --message <TEXT>
        ```

        四条映射规则（§7.2，逐条都有实测依据）：

        1. **成功信封没有 uid**（`data = {security_id, job_id, message}`）→ 反查 uid
           只能走 `chat()`，本函数不编一个假的出来。
        2. **`raw` 只放白名单键 `{"code": 0}`**（冻结清单第 15 条）。补页 §A 已证实
           `security_id` **不在**上游 `_redact_sensitive` 的敏感键白名单里——它会原样出现在
           信封里。把 `data` 整包塞进 `actions.response_json` 就是把敏感 ID 落库。
        3. **`ALREADY_GREETED` 不许当失败**：上游自己有第二层去重
           （`<data-dir>/cache/boss_agent.db` 的 `greet_records`），命中就**不发请求**直接
           返错误信封；`_parse_envelope` 已把它归一成 `ok:true`。这里只把它记成
           `already_greeted=True`，让编排层另计 `stats.already_greeted`（区分"真发出"与
           "平台早已打过"）。**不许再实现第二遍归一**。
        4. 错误码转成**编排层认识的那几个**：风控族 → `ACCOUNT_RISK`、限流族 → `GREET_LIMIT`
           （上游原始码保留在 `detail` 里，不丢信息）。`GREET_LIMIT` / `ACCOUNT_RISK` 是
           `dashboard` 里 break 与锁风险的判据，改名就等于闸门失效。
        """
        args = ["greet", str(security_id), str(job_id), "--message", str(message)]
        envelope = self._run_cli(*args)

        if envelope.get("ok"):
            data = envelope.get("data")
            payload = data if isinstance(data, dict) else {}
            # `_parse_envelope` 把 ALREADY_GREETED 归一成 ok=True 时，`last_error` 里
            # 留着 `error_code == "OK"` 这个痕迹——用它区分"真发出"与"早已打过"，
            # 不去改动 `_parse_envelope`（那是补页 §A 明确要求别动的一段）。
            last = self.last_error or {}
            already = last.get("error_code") == "OK"
            return {
                **ok_result(detail=str(payload.get("message") or "打招呼成功"), raw={"code": 0}),
                "already_greeted": bool(already),
            }

        # 失败：如实记进 last_error（含 command / code / message 三字段，同 detail 的收口），
        # 再映射成编排层认识的错误码。
        self._record_upstream_failure("greet", envelope)
        error = envelope.get("error") or {}
        raw_code = str(error.get("code") or "").strip() or None
        text = str(error.get("message") or "")[:200]
        normalized = normalize_error_code(raw_code)

        if normalized == "OK":  # 防御：理论上已在上面的成功分支被吃掉
            return {
                **ok_result(detail=text, raw={"code": 0}),
                "already_greeted": True,
            }
        if normalized == "UNCONFIRMED":
            # 发出了但无法确认：`error_code=None` 是既有约定，调用方**不许重试**
            return {
                **err_result(None, detail=text, raw={"code": raw_code}),
                "already_greeted": False,
            }
        if normalized == "RISK_LOCKED":
            return {
                **err_result(
                    "ACCOUNT_RISK", detail=f"{raw_code}: {text}", raw={"code": raw_code}
                ),
                "already_greeted": False,
            }
        if normalized == "LIMIT":
            # 限流族统一成 GREET_LIMIT（break 判据），原始码写进 detail
            return {
                **err_result(
                    "GREET_LIMIT", detail=f"{raw_code}: {text}", raw={"code": raw_code}
                ),
                "already_greeted": False,
            }
        if normalized == "AUTH":
            return {
                **err_result(raw_code or "AUTH_REQUIRED", detail=text, raw={"code": raw_code}),
                "already_greeted": False,
            }
        # 其他一律保留上游原始码（`HOOK_BLOCKED` / `UNKNOWN` → `NETWORK_ERROR` 之类），
        # 归成笼统的 UPSTREAM_ERROR 会把"被 hook 拦了"和"平台抽风"混成一件事。
        return {
            **err_result(raw_code or "UPSTREAM_ERROR", detail=text, raw={"code": raw_code}),
            "already_greeted": False,
        }

    def exchange(self, uid: str, type: str = "wechat") -> dict[str, Any]:
        """发起交换联系方式（微信/电话）。上游：`boss exchange <UID> --type wechat`。

        与 `greet` 的三个差别（写下来免得下次踩）：

        1. **入参是 `uid`**（会话对象），不是 security_id + job_id —— 不需要反查。
        2. **这是写操作**：调用方必须过闸门（`gate.exchange`），且只在"检测到 HR 主动提出
           交换"之后才允许调。本函数**自己不做闸门判断** —— 闸门在编排层
           （`api/dashboard.py` 的 followup）单点判定，两处各判一套必然打架。
        3. **成功 ≠ 拿到微信**：上游成功信封只代表"请求已发出"。对方同意后会回一张卡片
           或一条消息，真正的 `WECHAT_GOT` 只能靠回读 `chatmsg` + `extract_wechat` 识别
           （编排层已这么做）。所以这里**不返回、也不编造 wechat_id**。

        错误码映射沿用 greet 口径：风控族 → `ACCOUNT_RISK`（编排层据此锁风险并 break）、
        认证族 → 原码、限流族 → `RATE_LIMITED`、结果不确定 → `error_code=None`（不许重试）。
        """
        args = ["exchange", str(uid), "--type", str(type)]
        envelope = self._run_cli(*args)

        if envelope.get("ok"):
            data = envelope.get("data")
            payload = data if isinstance(data, dict) else {}
            return ok_result(
                detail=str(payload.get("message") or "交换请求已发出"),
                # 白名单键：整包 data 不落库（可能带 security_id 之类标识）
                raw={"code": 0},
            )

        self._record_upstream_failure("exchange", envelope)
        error = envelope.get("error") or {}
        raw_code = str(error.get("code") or "").strip() or None
        text = str(error.get("message") or "")[:200]
        normalized = normalize_error_code(raw_code)

        if normalized == "UNCONFIRMED":
            return err_result(None, detail=text, raw={"code": raw_code})
        if normalized == "RISK_LOCKED":
            return err_result("ACCOUNT_RISK", detail=f"{raw_code}: {text}", raw={"code": raw_code})
        if normalized == "AUTH":
            return err_result(raw_code or "AUTH_REQUIRED", detail=text, raw={"code": raw_code})
        if normalized == "LIMIT":
            return err_result("RATE_LIMITED", detail=f"{raw_code}: {text}", raw={"code": raw_code})
        return err_result(raw_code or "UPSTREAM_ERROR", detail=text, raw={"code": raw_code})

    def batch_greet(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """上游 `batch-greet` **本批不做**，这不是遗漏（§8，用户 2026-10-27 已确认撤回）。

        三条证据：① 它只有 `QUERY + --city/--salary/...`，**没有 `--url`**，而淄博不在
        上游 `CITY_CODES` 里 → 必返 `INVALID_PARAM: 未知城市`，一条都发不出去；
        ② 它内部自己 `search_jobs()` 再按 `--count` 取前 N 个，把我们的选岗逻辑整个绕过；
        ③ 它用上游自己的 `cache.is_greeted()` 去重，与我们 `actions` 表是**两套账**，
        第二套不可查、不进页面，出问题无法复盘。

        替代口径：`sweep` 永远走"单发 `greet` 循环"，节奏由 `daily_greet` + `schedule.sweep_at`
        决定，不再受上游 `--count 10` 约束。
        """
        raise NotImplementedError("上游 batch-greet 无 --url，本批不做")
