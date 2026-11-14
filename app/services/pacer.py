"""后台节奏推进器（闭环 1 的执行体）。

**它解决的问题**

改造前"发招呼"只有一条路：HTTP 点一次 → 一轮把额度内所有岗位发完，每条间隔 1.5–3 秒。
真机数据是 11 条 / 77 秒 / 同一句话，直接被平台打了风控锁。

本模块把"发"从"一次请求"变成"一条一条推进"：

```
醒 → 检查（暂停/风控/冷却/时段/额度）→ 发 1 条 → 睡 90–240s → 再醒
                    每发 3–5 条之后，长睡 15–30 分钟（轮间）
```

三条设计约束，都不是随手定的：

1. **单条驱动，不是整轮驱动。** 每次只调 `run_direct_greet_pass(limit=1)`。
   好处：停得掉（最多等一次发送，2 秒级）、错了立刻停（不用等一轮跑完）、
   重启接着发（`actions` 表的 claim 天然幂等）。
   代价：每条多一次 search。90 秒一次 search 完全在 20 次/分钟预算内，
   而且"每两分钟翻一下列表"本来就是真人的行为。
2. **冷却与熔断落盘**（`data/pacer_state.json`）。改造前风控锁只在内存里，
   重启服务就"看起来正常了"，而平台那边还在拒——7 天无人值守会变成"锁 6 天"。
   这里把 `cooldown_until` / `halted_until` 写文件，重启后照旧等。
3. **不复制业务逻辑。** 所有守卫（暂停、风控、额度、去重、claim、会话落库）
   仍然只在 `app/api/direct_greet.py` 里。本模块只负责"什么时候叫它"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app import models
from app.core import pacing as pacing_mod
from app.core import search_targets

log = logging.getLogger("jobpilot.pacer")

#: 无事可做时的兜底等待（秒）。别设太小：空转只是浪费 CPU，还会把日志刷满。
IDLE_SECONDS = 300

#: 被外部原因挡住（暂停 / 连接任务进行中 / 上游风控锁）时的复查间隔（秒）
BLOCKED_SECONDS = 60

#: 同一类错误连续这么多轮后，冷却拉长到这个秒数（30 分钟）。
#: 依据：2026-11-12 连续 8 轮 0 秒失败仍在每 5 分钟敲门，最后吃到风控。
ERROR_STREAK_COOLDOWN = 1800
ERROR_STREAK_LIMIT = 3

#: `TIMEOUT` 单独用更低的阈值。
#: 依据：2026-11-14 的链条是 TIMEOUT → TIMEOUT → ENVIRONMENT_RISK，
#: 只差一次就到 3 轮，但风控已经写进锁文件了。
#: TIMEOUT 表示页面根本打不开（不是网络抖动），此时继续敲门只会留下
#: "短时间内反复请求且全部失败"的形状。
TIMEOUT_STREAK_LIMIT = 2


def streak_limit_for(error_text: str | None) -> int:
    """同一类错误连续几轮之后该拉长冷却。

    抽成函数是为了能单测——这段判定一旦写错，后果是"该停的时候没停"，
    而它埋在 `_absorb` 里只能靠整轮模拟才测得到。
    """
    return TIMEOUT_STREAK_LIMIT if "TIMEOUT" in (error_text or "") else ERROR_STREAK_LIMIT


class _ShimRequest:
    """最小 Request 替身：`run_direct_greet_pass` 只用 `request.app.state.*`。"""

    def __init__(self, app: Any) -> None:
        self.app = app


def _iso(moment: datetime | date | None) -> str | None:
    """日期或时间 → ISO 串。`date` 没有 `timespec`，必须分开处理（踩过：TypeError）。"""
    if not moment:
        return None
    if isinstance(moment, datetime):
        return moment.isoformat(timespec="seconds")
    return moment.isoformat()


def _parse_iso(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class Pacer:
    """节奏推进器。一个进程一个实例（挂在 `app.state.pacer`）。"""

    def __init__(
        self,
        app: Any,
        *,
        now_fn: Any = datetime.now,
        rng: random.Random | None = None,
        state_path: str | Path | None = None,
    ) -> None:
        self.app = app
        self.cfg: dict[str, Any] = app.state.config
        self.raw_cfg: dict[str, Any] = dict(self.cfg.get("pacing") or {})
        self.enabled = bool(self.raw_cfg.get("enabled", False))
        self._now = now_fn
        self._rng = rng or random.Random()
        self._state_path = (
            Path(state_path)
            if state_path
            else Path(app.state.db_path).parent / "pacer_state.json"
        )

        self._pcfg: pacing_mod.PacerConfig | None = None
        self._pcfg_error: str | None = None
        if self.enabled:
            try:
                self._pcfg = pacing_mod.parse_config(
                    self.raw_cfg, daily_cap=int((self.cfg.get("quota") or {}).get("daily_greet") or 0)
                )
            except Exception as exc:  # 校验失败不拖垮服务：禁用节奏引擎，页面能看到原因
                self._pcfg_error = str(exc)
                self.enabled = False
                log.error("节奏引擎配置不合法，已禁用：%s", exc)

        self._task: asyncio.Task[Any] | None = None
        self._stop: asyncio.Event | None = None
        self.running = False

        #: 【闭环 1 · 2026-11-04】"这一拍正在发"的独立标记。
        #: 不能用 `_task.done()` 代替：循环大部分时间都在 sleep 等间隔，页面需要区分
        #: "在等下一次"（正常）与"正在发、已经发了两分钟"（可能卡住）。
        self._in_pass = False
        self._pass_started_at: datetime | None = None

        self._round_left = 0
        self._err_signature: str | None = None
        self._err_streak = 0
        #: 【闭环 1 · 2026-11-04】搜索页码游标：每次 pass 只翻一页，跨 pass 轮转 1..pages。
        #: 起因是实测：一个进程里连着翻 5 页，第 2 页就吃 `ENVIRONMENT_RISK`；
        #: 同样的页单独请求却正常（77s / 177s）。连页 = 爬虫特征，必须摊开。
        self._page_cursor = 1
        self._next_at: datetime | None = None
        self._next_reason = "idle"
        self.cooldown_until: datetime | None = None
        self.halted_until: datetime | None = None
        self.last_sent_at: datetime | None = None
        self.last_verdict: str | None = None
        #: 【2026-11-04】裁决时间。`last_verdict` 是**持久化**的，`start()` 又只返回
        #: 当前快照 —— 于是"上一次运行留下的 error"看起来像"这一次刚失败"。
        #: 真机现场：20:32 点开始，页面显示 error（其实是 20:19 那条），误导了判断。
        self.last_verdict_at: datetime | None = None
        self.last_error: str | None = None
        self.last_stats: dict[str, Any] | None = None
        #: 【2026-11-04 借社区口径】当日命中风控次数。首次只冷却 30–60 分钟，
        #: 同日第 `halt_after_risks` 次才升到 24h 熔断（一次偶发不该废掉整天）。
        self._risk_hits_today = 0
        self._risk_day: date | None = None
        #: 当日发送硬上限（`start(cap=3)` = 小批量试跑）。None = 按 ramp 目标。
        self._cap_today: int | None = None
        self._cap_day: date | None = None
        #: 【2026-11-13】动态加量后的日目标。**必须落盘**：
        #:  不落盘的话 `_target_today()` 每次还是返回 ramp 值，
        #:  `used >= target` 会立刻成立，于是每一轮都重新加一档 ——
        #:  实测会在一个晚上从 25 一路发到 50，这不是"加量"，是失控。
        self._extended_target: int | None = None
        self._extended_day: date | None = None
        self._load_state()

    # ------------------------------------------------------------------ #
    # 状态持久化
    # ------------------------------------------------------------------ #
    def _load_state(self) -> None:
        """读冷却/熔断时间戳。读不出来不是致命错（首次运行就没有文件）。"""
        path = self._state_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("节奏状态文件读不出来，按全新状态处理：%s（%s）", path, exc)
            return
        if not isinstance(data, dict):
            return
        self.cooldown_until = _parse_iso(data.get("cooldown_until"))
        self.halted_until = _parse_iso(data.get("halted_until"))
        self.last_sent_at = _parse_iso(data.get("last_sent_at"))
        self.last_verdict = data.get("last_verdict")
        self.last_verdict_at = _parse_iso(data.get("last_verdict_at"))
        self.last_error = data.get("last_error")
        raw_hits = data.get("risk_hits_today")
        self._risk_hits_today = int(raw_hits) if isinstance(raw_hits, int) else 0
        risk_day = _parse_iso(data.get("risk_day"))
        self._risk_day = risk_day.date() if risk_day else None
        raw_cap = data.get("cap_today")
        self._cap_today = int(raw_cap) if isinstance(raw_cap, int) else None
        cap_day = _parse_iso(data.get("cap_day"))
        self._cap_day = cap_day.date() if cap_day else None
        raw_ext = data.get("extended_target")
        self._extended_target = int(raw_ext) if isinstance(raw_ext, int) else None
        ext_day = _parse_iso(data.get("extended_day"))
        self._extended_day = ext_day.date() if ext_day else None

    def _save_state(self) -> None:
        payload = {
            "cooldown_until": _iso(self.cooldown_until),
            "halted_until": _iso(self.halted_until),
            "last_sent_at": _iso(self.last_sent_at),
            "last_verdict": self.last_verdict,
            "last_verdict_at": _iso(self.last_verdict_at),
            "last_error": self.last_error,
            # 当日风控次数与试验上限都要落盘：重启服务不该"清零重来"，
            # 否则重启就成了绕过熔断的后门（和改造前"重启就看起来正常了"同一个坑）。
            "risk_hits_today": self._risk_hits_today,
            "risk_day": _iso(self._risk_day),
            "cap_today": self._cap_today,
            "cap_day": _iso(self._cap_day),
            "extended_target": self._extended_target,
            "extended_day": _iso(self._extended_day),
            "saved_at": _iso(self._now()),
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:  # 写不进去不许影响发送
            log.warning("节奏状态写不进去：%s（%s）", self._state_path, exc)

    # ------------------------------------------------------------------ #
    # 日目标与用量
    # ------------------------------------------------------------------ #
    def _today_utc(self) -> date:
        """用 **UTC 日期**，与 `models.count_actions_today` 的 `date('now')` 对齐。

        时段从 09:00 开始，本地日期与 UTC 日期在整个发送窗口内是同一个，
        所以这里不会出现"目标换了天、用量还挂在昨天"的错位。
        """
        return datetime.now(timezone.utc).date()

    def _used_today(self) -> int:
        conn = models.connect(self.app.state.db_path)
        try:
            return models.count_actions_today(conn, "greet", ok_only=True)
        finally:
            conn.close()

    def _target_today(self) -> int:
        if self._pcfg is None:
            return 0
        quota = self.cfg.get("quota") or {}
        target = pacing_mod.ramp_target(
            self._pcfg, self._today_utc(), daily_cap=int(quota.get("daily_greet") or 0)
        )
        # 小批量试跑：`start(cap=3)` 当天最多 3 条。只收紧、不放大。
        if self._cap_today is not None and self._cap_day == self._today_utc():
            target = min(target, self._cap_today)
        # 今天已经动态加过量 → 以加量后的值为准（否则每轮都会重新加，直到硬上限）
        if self._extended_day == self._today_utc() and self._extended_target:
            hard_max = int(quota.get("daily_greet_max") or quota.get("daily_greet") or 0)
            target = max(target, self._extended_target if not hard_max else min(self._extended_target, hard_max))
        return target

    def _extend_target(self, now: datetime, target: int) -> int:
        """时段还有余量就把目标往上加一档，否则原样返回（2026-11-13 用户要求）。

        规则（三条都必须满足）：
        1. 还没到 `quota.daily_greet_max` 硬上限
        2. 当前时段剩余时间**够发一条**
        3. 每次只加 `pacing.extend_step`（默认 5），不一次跳到上限

        为什么不做成"一次性算好"：加量应该跟着**实际剩余时间**走。
        15:00 用完 25 条和 17:50 用完 25 条，剩下的时间差很多，能加的量也差很多。
        """
        assert self._pcfg is not None
        pacing_cfg = self.cfg.get("pacing") or {}
        quota = self.cfg.get("quota") or {}

        hard_max = int(quota.get("daily_greet_max") or quota.get("daily_greet") or target)
        if target >= hard_max:
            return target

        # 当前时段剩余秒数（不在任何时段内就返回 0）
        left = 0
        for segment in self._pcfg.segments:
            end = datetime.combine(now.date(), segment.end)
            if now < end:
                left = int((end - now).total_seconds())
                break
        if left <= 0:
            return target

        # 一条大致要多久：发送间隔均值 + 一批里摊销的列表加载时间
        batch = max(int(pacing_cfg.get("send_batch") or 1), 1)
        per_send = (self._pcfg.interval_lo + self._pcfg.interval_hi) / 2
        search_cost = 120.0 / batch  # 上游单页 67–177 秒，取 120 秒摊到一批上
        if left < per_send + search_cost:
            return target

        step = max(int(pacing_cfg.get("extend_step") or 5), 1)
        can_add = int(left // max(per_send + search_cost, 60))
        if can_add < 1:
            return target

        # 以"今天已加到的值"为基准往上加，并**立刻落盘** ——
        # 否则下一轮又从 ramp 基线算起，等于每次都解锁一档。
        base = target
        if self._extended_day == self._today_utc() and self._extended_target:
            base = self._extended_target
        new_target = min(base + min(step, can_add), hard_max)
        if new_target <= base:
            return base
        self._extended_target = new_target
        self._extended_day = self._today_utc()
        self._save_state()
        return new_target

    def _count_risk_today(self, now: datetime) -> int:
        """记一次风控命中，返回今天累计第几次（跨天自动归零）。"""
        today = self._today_utc()
        if self._risk_day != today:
            self._risk_day = today
            self._risk_hits_today = 0
        self._risk_hits_today += 1
        log.warning("今日第 %s 次命中风控（%s）", self._risk_hits_today, now.isoformat(timespec="seconds"))
        return self._risk_hits_today

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self, cap: int | None = None) -> dict[str, Any]:
        """启动后台循环。已在跑 → 幂等返回当前状态。

        `cap`：今日发送硬上限（小批量试跑用，例如 `cap=3`）。跨天自动失效（落盘里带日期）。

        【2026-11-08】`cap` **在运行中也能改**：先落 cap、再判"已在跑"。
        否则想从 5 条加到 15 条只能重启服务（cap 是持久化的，重启也带回来）。
        """
        if not self.enabled or self._pcfg is None:
            return {
                "ok": False,
                "error": self._pcfg_error or "节奏引擎未启用（config.yaml 的 pacing.enabled 为 False）",
            }
        if cap is not None:
            self._cap_today = max(int(cap), 0)
            self._cap_day = self._today_utc()
            self._save_state()
        if self.running and self._task and not self._task.done():
            return {"ok": True, "already_running": True, "status": self.status()}
        # 【闭环 1 · 2026-11-04】`stop()` 之后上一轮可能还在"收尾"：进行中的那一次发送
        # 无法中断（线程里是同步 CLI 调用）。这时**不许**再起一个循环 —— 两个循环会
        # 双倍发送，`_round_left` 还会互相踩。等它返回，或者重启服务。
        if self._task is not None and not self._task.done():
            return {
                "ok": False,
                "error": "上一轮仍在收尾（进行中的一次发送不能中断）：等它返回后再启动",
                "status": self.status(),
            }
        self._stop = asyncio.Event()
        self.running = True
        self._round_left = 0
        self._err_signature: str | None = None
        self._err_streak = 0
        self._next_at = self._now()
        self._next_reason = "start"
        self._task = asyncio.create_task(self._loop())
        # 用 done 回调清引用，而不是在 `_loop` 末尾清：回调持有 task 的强引用，
        # 避免"任务还挂着但没人引用"被 GC 回收（那会静默吃掉本轮结果）。
        self._task.add_done_callback(self._forget_task)
        return {"ok": True, "already_running": False, "status": self.status()}

    def _forget_task(self, task: asyncio.Task[Any]) -> None:
        if self._task is task:
            self._task = None

    def stop(self, *, reason: str = "用户停止") -> dict[str, Any]:
        was_running = self.running
        self.running = False
        if self._stop is not None:
            self._stop.set()
        self._next_at = None
        self._next_reason = "stopped"
        # 【闭环 1 · 2026-11-04 事故】**不要**把 `self._task` 置 None。
        #   挂起中的 task 失去最后一个强引用后会被 GC 回收（asyncio 只保留弱引用），
        #   那一轮的结果 —— 包括"命中风控"和 24 小时熔断落盘 —— 会被静默吞掉。
        #   真机现场：`last_verdict` 永远 null、`pacer_state.json` 从没生成，
        #   而平台那边其实已经重新上锁了。清引用交给 `_forget_task`。
        log.info("节奏引擎已停止：%s", reason)
        return {"ok": True, "was_running": was_running, "status": self.status()}

    async def _loop(self) -> None:
        assert self._stop is not None
        try:
            while not self._stop.is_set() and self.running:
                try:
                    delay = await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # 循环自己绝不许死：死了就没人再推进
                    log.exception("节奏引擎 tick 异常")
                    delay = BLOCKED_SECONDS
                self._next_at = self._now() + timedelta(seconds=max(int(delay), 1))
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=max(int(delay), 1))
                except asyncio.TimeoutError:
                    pass
        finally:
            self.running = False
            self._in_pass = False

    async def _tick(self) -> int:
        """一次决策。返回"下次醒来还有多少秒"。"""
        assert self._pcfg is not None
        now = self._now()

        # 1) 全局暂停（与风控锁独立，语义见 dashboard）
        if bool(getattr(self.app.state, "paused", False)):
            self._next_reason = "paused"
            return BLOCKED_SECONDS

        # 2) 上游风控锁：不是"等一等"，是要人工处理。回报告诉页面
        if bool(getattr(self.app.state, "risk_locked", False)):
            self._next_reason = "risk_locked"
            self._halt(now, reason="上游风控锁仍在（cdp_risk_lock.json），节奏已停")
            return int(self._pcfg.halt_seconds)

        # 3) 熔断 / 冷却：先等，不发
        if self.halted_until and now < self.halted_until:
            self._next_reason = "halt"
            return int((self.halted_until - now).total_seconds())
        if self.cooldown_until and now < self.cooldown_until:
            self._next_reason = "cooldown"
            return int((self.cooldown_until - now).total_seconds())

        # 4) 时段外：等下一个窗口
        if pacing_mod.segment_index(now, self._pcfg.segments) is None:
            upcoming = pacing_mod.next_window_start(now, self._pcfg.segments)
            if upcoming is None:
                self._next_reason = "idle"
                return IDLE_SECONDS
            self._next_reason = "window"
            return int((upcoming - now).total_seconds())

        # 5) 今日目标已达成 —— 但**先看看还有没有时间**。
        #    【2026-11-13 用户要求】不是"跑满 25 就收工"，而是"跑满之后时段还剩时间就继续加"，
        #    一路加到 daily_greet_max 或时段结束。原实现直接 done 等到明天，白扔下午/晚上。
        target = self._target_today()
        used = self._used_today()
        if used >= target:
            extended = self._extend_target(now, target)
            if extended > target:
                log.info("今日已达 %s 条，时段还有余量：目标提到 %s 条继续跑", used, extended)
                target = extended
            else:
                first = self._pcfg.segments[0]
                tomorrow = datetime.combine(now.date() + timedelta(days=1), first.start)
                self._next_reason = "done"
                self._next_at = tomorrow
                return max(int((tomorrow - now).total_seconds()), 60)

        # 6) 连接任务进行中：让位（连接与业务互斥）
        manager = getattr(self.app.state, "connect", None)
        blocked = manager.acquire_business() if manager is not None else None
        if blocked:
            self._next_reason = "blocked"
            return BLOCKED_SECONDS

        try:
            self._in_pass = True
            self._pass_started_at = now
            result = await asyncio.to_thread(self._pass_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 【闭环 1 · 2026-11-04】异常也必须留下裁决。
            #   原来异常直接冒到 `_loop` 的兜底里，只打一条日志：`last_verdict` 永远是 null，
            #   页面上"卡住了"和"根本没跑"长得一模一样 —— 真机排障就是被这一点拖住的。
            log.exception("节奏引擎本轮异常")
            result = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "verdict": "error",
                "stats": {},
            }
        finally:
            self._in_pass = False
            if manager is not None:
                manager.release_business()

        return self._absorb(result, now)

    # ------------------------------------------------------------------ #
    # 执行一条
    # ------------------------------------------------------------------ #
    def _pass_once(self) -> dict[str, Any]:
        """在线程里跑一次「一次搜索 + 发一批」。自建连接，不碰事件循环里的任何东西。

        【2026-11-13 请求形状改造】原来每次只发 1 条（`limit=1`），
        于是每发一条都要重新加载一次完整列表页（上游单页 67–177 秒），
        50 条 = 50 次列表重载。平台看到的是"同一个页面每 2–4 分钟被重载一次，
        每次都立刻调一次沟通接口，中间没有滚动/停留/点击" —— 这是最不像人的形状。

        现在：一次搜索领 `pacing.send_batch` 条（默认 1 = 老行为），
        批内由 `send_gap` 保证 90–240 秒间隔，所以**并没有变激进**，
        只是把"重载列表"这一步摊到多条上了。
        """
        from app.api.direct_greet import run_direct_greet_pass  # 延迟 import：避免环

        pacing_cfg = self.cfg.get("pacing") or {}
        batch = max(int(pacing_cfg.get("send_batch") or 1), 1)
        url, page = self._next_search_target()
        conn = models.connect(self.app.state.db_path)
        try:
            return run_direct_greet_pass(
                request=_ShimRequest(self.app),
                db=conn,
                cfg=self.cfg,
                limit=batch,
                page=page,
                url=url,
                send_gap=(self._pcfg.interval_lo, self._pcfg.interval_hi) if self._pcfg else None,
            )
        finally:
            conn.close()

    def _search_targets(self) -> list[tuple[str | None, int]]:
        """所有 (关键词 URL, 页码) 组合，按"先换页、再换词"排。

        为什么要多关键词：claim 是**永久**的（发过就再也不发），所以候选池是一次性
        消耗的。单个关键词在淄博只有约 30 条（3 页），一天 15 条 → 第二天就见底，
        第三天整天 `skipped`。换关键词是唯一能把池子做大的低风险手段：
        它不增加请求频率，只把请求摊到不同查询上（对人来说也更自然）。
        """
        options = self.cfg.get("adapter_options") or {}
        pages = max(int(options.get("pages") or 1), 1)
        target = self.cfg.get("target") or {}
        # 【闭环 4 · 2026-11-12】统一走 `search_targets.build_urls`：声明式
        # `keywords × cities` 展开，避免"手写 9 条 URL 且城市写错"那类静默故障。
        urls = search_targets.build_urls(target) or [""]
        return [(url or None, page) for url in urls for page in range(1, pages + 1)]

    def _next_search_target(self) -> tuple[str | None, int]:
        """下一个 (url, page)。跨 pass 轮转，重启从第一组开始。"""
        targets = self._search_targets()
        index = min(max(self._page_cursor - 1, 0), len(targets) - 1)
        self._page_cursor = 1 if index + 1 >= len(targets) else index + 2
        return targets[index]

    def _peek_target(self) -> tuple[str | None, int]:
        """看一眼下一组是什么（不动游标），只给 `status()` 用。"""
        targets = self._search_targets()
        index = min(max(self._page_cursor - 1, 0), len(targets) - 1)
        return targets[index]

    def _next_search_page(self) -> int:
        """下一个要搜的页码（保留旧接口：只关心页的地方还在用）。"""
        return self._next_search_target()[1]

    def _track_error_streak(self, verdict: str) -> int:
        """同一类错误连续第几轮。返回连击数（非 error 判定会清零）。

        【2026-11-12 事故】14:45–15:20 连续 8 轮 **0 秒失败**
        （`attempt to write a readonly database`），而引擎每 5 分钟照敲一次平台。
        这些请求注定失败，却构成了"短时间高频请求"的痕迹 —— 15:31 吃到风控。

        连击到第 3 轮就该停下想一想，而不是继续按原节奏敲门。
        """
        current = (self.last_error or "")[:40]
        signature = f"{verdict}:{current}"
        if verdict == "error" and signature == self._err_signature:
            self._err_streak += 1
        elif verdict == "error":
            self._err_signature = signature
            self._err_streak = 1
        else:
            self._err_signature = None
            self._err_streak = 0
        return self._err_streak

    def _absorb(self, result: dict[str, Any], now: datetime) -> int:
        """把一轮结果变成"下一步睡多久"。"""
        assert self._pcfg is not None
        verdict = str(result.get("verdict") or ("ok" if result.get("ok") else "error"))
        stats = result.get("stats") or {}
        self.last_verdict = verdict
        self.last_verdict_at = now
        self.last_stats = stats
        self.last_error = None if result.get("ok") else str(result.get("error") or "")

        if int(stats.get("sent_new") or 0) > 0:
            self.last_sent_at = now

        # 风控 → 先冷却试跑，同日累计到阈值才 24h 熔断
        if pacing_mod.is_halt(verdict):
            hits = self._count_risk_today(now)
            if hits >= int(self._pcfg.halt_after_risks):
                # 同日已经不是第一次了：偶发解释不通，躺满 24h 等人处理
                self._halt(
                    now,
                    reason=f"{self.last_error or '命中风控'}（今日第 {hits} 次，已熔断）",
                )
                return int(self._pcfg.halt_seconds)
            # 首次：冷却 30–60 分钟后小批量试跑（借社区口径，见 pacing.DEFAULT_RISK_COOLDOWN）
            cool = self._pcfg.next_risk_cooldown(self._rng)
            self.cooldown_until = now + timedelta(seconds=cool)
            self._next_reason = "cooldown"
            self._save_state()
            log.warning(
                "命中风控（今日第 %s 次）：冷却 %ss 后小批量试跑；同日第 %s 次才 24h 熔断",
                hits,
                cool,
                self._pcfg.halt_after_risks,
            )
            return cool

        # 【2026-11-08 事故】登录态失效 → **必须停**，不能"冷却一小时再试"。
        #   现场：09:00 第 1 条 AUTH_EXPIRED、10:53 第 2 条又 AUTH_EXPIRED（冷却一小时后重试），
        #   14:00 变成 ENVIRONMENT_RISK + 平台写回锁 + 24h 熔断。
        #   道理很简单：会话失效后重试**永远不会成功**，而每次重试都是"拿过期凭证敲平台"的痕迹。
        #   所以这里停循环、不设熔断时间：扫码之后 `start` 一次就能继续。
        if verdict == "auth":
            self.last_error = (
                "登录态已失效（AUTH_EXPIRED）：在浏览器里扫码重新登录，然后重新启动节奏引擎。"
                "期间不会再发任何请求"
            )
            self._next_reason = "auth"
            self.cooldown_until = None
            self.running = False
            if self._stop is not None:
                self._stop.set()
            self._save_state()
            log.error("登录态失效，节奏已停（不自动重试）：扫码后需手动重新启动")
            return int(self._pcfg.halt_seconds)

        streak = self._track_error_streak(verdict)
        cooldown_s = pacing_mod.cooldown_for(verdict, self._pcfg)
        # 同一类错误连续多轮：拉长到 30 分钟。
        # 这类失败（DB 锁、CLI 卡死、参数非法）**重试不会自愈**，继续按 5 分钟敲只是攒痕迹。
        #
        # 【2026-11-14】TIMEOUT 的阈值单独降到 2 轮。现场链条：
        #   14:47 TIMEOUT（300 秒页面打不开）→ 14:57 TIMEOUT → 15:02 ENVIRONMENT_RISK。
        #   只差一次就到 3 轮阈值，而风控已经落下来了。
        # 为什么 TIMEOUT 该更早停：它表示**页面根本打不开**，不是偶发网络抖动。
        # 页面打不开还按 5–10 分钟反复敲，平台看到的正是"短时间内反复请求且全部失败"，
        # 那是最容易被判环境的形状。
        is_timeout = "TIMEOUT" in (self.last_error or "")
        limit = streak_limit_for(self.last_error)
        if streak >= limit and cooldown_s < ERROR_STREAK_COOLDOWN:
            cooldown_s = ERROR_STREAK_COOLDOWN
            log.error(
                "同一错误连续 %s 轮（%s%s）：改为长冷却 %ss，停止空转敲门",
                streak,
                "TIMEOUT " if is_timeout else "",
                (self.last_error or "")[:60],
                cooldown_s,
            )
        if cooldown_s > 0:
            self.cooldown_until = now + timedelta(seconds=cooldown_s)
            self._next_reason = "cooldown"
            self._save_state()
            log.warning("节奏冷却 %ss（verdict=%s）：%s", cooldown_s, verdict, self.last_error)
            return cooldown_s

        # 没发出去（列表里没有新岗位 / 额度刚好用完）：睡长一点，别空转
        if verdict == "skipped":
            self._next_reason = "skipped"
            return max(int(self._pcfg.next_round_gap(self._rng)), BLOCKED_SECONDS)

        # 正常发出：轮内用短间隔，轮末用长休息
        #
        # 【2026-11-13 请求形状改造】一批可能发多条，轮次计数必须按**实际发出数**递减。
        # 原来写死 `-= 1`：一批发 4 条只扣 1 次，一轮的边界会往后拖 4 倍，
        # 该有的"轮间长休息"就永远等不到 —— 这比请求数本身更影响节奏。
        sent = int(stats.get("sent_new") or 0)
        if self._round_left <= 0:
            self._round_left = self._pcfg.round_size(self._rng)
        self._round_left -= max(sent, 1)
        if self._round_left <= 0:
            self._next_reason = "round_gap"
            delay = self._pcfg.next_round_gap(self._rng)
        else:
            self._next_reason = "interval"
            delay = self._pcfg.next_interval(self._rng)
        self._save_state()
        log.info(
            "本轮发出 %s 条（今日 %s/%s），下一次 %ss 后（%s）",
            sent,
            self._used_today(),
            self._target_today(),
            delay,
            self._next_reason,
        )
        return delay

    def _halt(self, now: datetime, *, reason: str) -> None:
        assert self._pcfg is not None
        self.halted_until = now + timedelta(seconds=int(self._pcfg.halt_seconds))
        self.cooldown_until = None
        self.last_error = reason
        self._next_reason = "halt"
        self._save_state()
        log.error("节奏熔断 %ss：%s", self._pcfg.halt_seconds, reason)

    # ------------------------------------------------------------------ #
    # 对外状态
    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        """页面/接口用的快照。**只用内存 + 一次 DB count**，不发任何上游请求。"""
        now = self._now()
        pcfg = self._pcfg
        target = self._target_today()
        try:
            used = self._used_today()
        except Exception:  # DB 暂时打不开也不该让页面 500
            used = 0

        segment_index = (
            pacing_mod.segment_index(now, pcfg.segments) if pcfg is not None else None
        )
        share = (
            pacing_mod.segments_quota_share(target, len(pcfg.segments)) if pcfg is not None else []
        )

        return {
            "enabled": self.enabled,
            "running": bool(self.running),
            "config_error": self._pcfg_error,
            "day": self._today_utc().isoformat(),
            "used": used,
            "target": target,
            "remaining": max(target - used, 0),
            #: 今日试验上限（`start(cap=3)` 才有值）；None = 按 ramp 目标跑
            "cap_today": self._cap_today if self._cap_day == self._today_utc() else None,
            #: 今日命中风控次数（第 `halt_after_risks` 次才 24h 熔断）
            "risk_hits_today": self._risk_hits_today if self._risk_day == self._today_utc() else 0,
            "segment": str(pcfg.segments[segment_index]) if segment_index is not None else None,
            "segment_index": segment_index,
            "segments": [str(s) for s in pcfg.segments] if pcfg is not None else [],
            "segment_share": share,
            "interval_s": [pcfg.interval_lo, pcfg.interval_hi] if pcfg is not None else [],
            "round": [pcfg.round_min, pcfg.round_max] if pcfg is not None else [],
            "round_left": max(self._round_left, 0),
            "ramp": list(pcfg.ramp) if pcfg is not None else [],
            "next_at": _iso(self._next_at),
            "next_reason": self._next_reason,
            # 【闭环 1 · 2026-11-04】`inflight` = 这一拍正在发（可能卡住）；
            # `last_verdict` 只在一轮**结束**后才有值，轮内查必然是 null，别再当成故障。
            "inflight": bool(self._in_pass),
            "pass_started_at": _iso(self._pass_started_at),
            #: 下一条招呼会用哪一组（关键词 URL + 页码）。用来确认"没有连页、也没死磕同一个词"。
            "next_search_page": self._peek_target()[1],
            "next_search_url": self._peek_target()[0],
            "cooldown_until": _iso(self.cooldown_until),
            "halted_until": _iso(self.halted_until),
            "last_verdict": self.last_verdict,
            "last_verdict_at": _iso(self.last_verdict_at),
            "last_error": self.last_error,
            "last_sent_at": _iso(self.last_sent_at),
            "pool_size": len(getattr(self.app.state, "greet_pool", []) or []),
            "state_path": str(self._state_path),
        }


def build_pacer(app: Any, state_path: str | Path | None = None) -> Pacer:
    """按 `app.state.config` 建实例（`main.py` 装配时调用）。

    `state_path` 给多账号用：每个账号一份 `pacer_state_<id>.json`，
    否则同目录下多个账号会共用一份熔断/冷却状态（串号）。
    """
    return Pacer(app, state_path=state_path)
