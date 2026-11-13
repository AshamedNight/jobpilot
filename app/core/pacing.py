"""节奏引擎（闭环 1）—— 把"发得快"改成"发得像人"。

**这个模块只有纯函数，没有 IO、没有全局状态。** 所有会睡、会写文件、会调适配器的
东西都在 `app/services/pacer.py`。这样"时段算得对不对""间隔够不够"可以离线单测，
不用起服务、不用等 3 小时。

三个概念分清楚，不要把配置写成一个数字：

- **日目标**（`ramp_target`）：今天打算发多少条。按周爬坡，且永远不会超过
  `quota.daily_greet`（那是硬额度，页面上的分母）。
- **单轮条数**（`round_size`）：一次连续发几条。**3–5 条**，不是"发到额度用完"。
- **相邻间隔**（`sample_interval`）：两条之间的静默。**90–240 秒**，不是 3 秒。

真机事故的根因就是这三个数被压成了一个：77 秒发 11 条 = 单轮无限、间隔 3 秒。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Sequence

from app.config import StartupError

#: 单轮条数的硬边界。上限 5 来自"连发 5 条 + 每条一次 search/chat"仍在
#: 上游 20 次/分钟共享预算内；下限 3 是为了让"一轮"有意义（否则退化成单条驱动）。
ROUND_MIN_HARD = 1
ROUND_MAX_HARD = 8

#: 间隔硬下限（秒）。低于 60 秒的间隔在行为画像上和机器人没有区别，
#: 配置里写 10 也不会生效——启动就报错，不给"悄悄调快"的机会。
INTERVAL_FLOOR = 60

#: 判定码 → 冷却时长（秒）。`risk` 是熔断不是冷却，单独走 `halt`。
DEFAULT_COOLDOWN_SECONDS: dict[str, int] = {
    "limit": 45 * 60,        # 限流：等一个窗口，通常 30–60 分钟
    "unconfirmed": 10 * 60,  # 结果不确定：别立刻重试，但不用等太久
    "auth": 60 * 60,         # 登录态问题：1 小时内重试没意义，等人处理
    "error": 5 * 60,         # 普通失败（岗位下架等）：小退避
    # 【闭环 1 · 2026-11-04】上游 CLI 超时。本轮结果不可信，且进程可能已 fail-closed，
    # 给 10 分钟退避，不要用 5 分钟反复撞同一个卡点。
    "timeout": 10 * 60,
}

#: 单次熔断时长（秒）。命中风控后 24 小时不许再发，重启也不解除（状态落盘）。
DEFAULT_HALT_SECONDS = 24 * 60 * 60

#: 【2026-11-04 借自社区排障口径】命中风控后的**首次**处置：
#: 先冷却 30–60 分钟、随后小批量（3–5 条）试跑，而不是直接躺一天。
#: 依据：`drydraft/boss-recruit-kit` 的 code 37 修复流程就是"冷却 30–60 分钟后小批量试跑"。
#: 同日**第二次**命中才升到 24h 熔断（见 `halt_after_risks`）——
#: 一次偶发就锁一天，会把验证窗口白扔掉；两次还中，说明不是偶发。
DEFAULT_RISK_COOLDOWN: tuple[int, int] = (30 * 60, 60 * 60)

#: 同一天第几次命中风控才升级成 24h 熔断。1 = 恢复到"一次就熔断"的旧行为。
DEFAULT_HALT_AFTER_RISKS = 2


@dataclass(frozen=True)
class Segment:
    """一个可发送时段（含左端点，不含右端点：12:00 整点不再发）。"""

    start: time
    end: time

    def contains(self, moment: time) -> bool:
        return self.start <= moment < self.end

    def __str__(self) -> str:
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"


@dataclass(frozen=True)
class PacerConfig:
    """节奏参数（全部来自 `config.yaml` 的 `pacing` 段，已校验）。

    用 dataclass 而不是裸 dict：pacer 每个 tick 都要读这些值，
    键名写错在 dict 里是 `KeyError`（运行期才炸），在这里是构造期就炸。
    """

    segments: tuple[Segment, ...]
    interval_lo: int
    interval_hi: int
    round_min: int
    round_max: int
    round_gap_lo: int
    round_gap_hi: int
    ramp: tuple[int, ...]
    ramp_start: date | None
    cooldown: dict[str, int]
    halt_seconds: int
    #: 命中风控的首次冷却区间（秒），以及"同日第几次命中才 24h 熔断"。
    risk_cooldown: tuple[int, int] = DEFAULT_RISK_COOLDOWN
    halt_after_risks: int = DEFAULT_HALT_AFTER_RISKS
    #: 每几“天”升一档。默认 7（按周），设小就是更快的阶梯。
    #: 可配置的理由：撞过风控之后，"按周日历爬坡"与真实状态完全脱节 ——
    #: 我们连续几天一条没发出去，日历却照样进下一档。
    #: 必须放在**所有无默认值字段之后**，否则 dataclass 会报 non-default follows default。
    ramp_step_days: int = 7

    # -- 派生 ---------------------------------------------------------- #
    def round_size(self, rng: random.Random) -> int:
        """本轮连发几条。区间内均匀取整，两端都可达。"""
        if self.round_max <= self.round_min:
            return self.round_min
        return rng.randint(self.round_min, self.round_max)

    def next_risk_cooldown(self, rng: random.Random) -> int:
        """命中风控后先歇多久（默认 30–60 分钟），歇完小批量试跑。"""
        lo, hi = self.risk_cooldown
        if hi <= lo:
            return lo
        return rng.randint(lo, hi)

    def next_interval(self, rng: random.Random) -> int:
        """同一轮里两条之间的静默秒数（90–240s）。"""
        if self.interval_hi <= self.interval_lo:
            return self.interval_lo
        return rng.randint(self.interval_lo, self.interval_hi)

    def next_round_gap(self, rng: random.Random) -> int:
        """一轮结束后的长休息（默认 15–30 分钟）。

        没有这个，50 条就是"每 2.5 分钟一条、连续 3 小时"——仍然是一条直线。
        真实求职者是"看几分钟列表 → 挑 3-5 个 → 停一会儿 → 再回来"。
        """
        if self.round_gap_hi <= self.round_gap_lo:
            return self.round_gap_lo
        return rng.randint(self.round_gap_lo, self.round_gap_hi)


# --------------------------------------------------------------------------- #
# 解析与校验
# --------------------------------------------------------------------------- #
def _parse_hhmm(raw: Any, field: str) -> time:
    try:
        return datetime.strptime(str(raw).strip(), "%H:%M").time()
    except (TypeError, ValueError) as exc:
        raise StartupError(
            f"配置错误：pacing.{field}={raw!r} 不是 HH:MM 格式的时间"
        ) from exc


def _parse_ymd(raw: Any) -> date | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise StartupError(
            f"配置错误：pacing.ramp_start={raw!r} 不是 YYYY-MM-DD 格式的日期"
        ) from exc


def parse_segments(raw: Any) -> tuple[Segment, ...]:
    """`[["09:00","12:00"], ...]` → `(Segment, ...)`。空表 → 空元组（= 全天不发送）。"""
    if raw is None:
        return ()
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes)):
        raise StartupError("配置错误：pacing.segments 必须是 [[\"09:00\",\"12:00\"], ...] 这样的列表")

    segments: list[Segment] = []
    for index, item in enumerate(raw):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise StartupError(
                f"配置错误：pacing.segments 第 {index + 1} 段必须是两个时间的列表，当前 {item!r}"
            )
        start = _parse_hhmm(item[0], f"segments[{index}][0]")
        end = _parse_hhmm(item[1], f"segments[{index}][1]")
        if end <= start:
            raise StartupError(
                f"配置错误：pacing.segments 第 {index + 1} 段的结束时间不晚于开始时间"
                f"（{start.strftime('%H:%M')}-{end.strftime('%H:%M')}）。跨零点的时段不支持，"
                f"请拆成两段。"
            )
        segments.append(Segment(start, end))

    ordered = sorted(segments, key=lambda s: s.start)
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt.start < prev.end:
            raise StartupError(
                f"配置错误：pacing.segments 有重叠时段（{prev} 与 {nxt}）。"
                f"重叠会让同一条被算两次进度，请拆开。"
            )
    return tuple(ordered)


def _positive_pair(raw: Any, field: str, floor: int) -> tuple[int, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise StartupError(f"配置错误：pacing.{field} 必须是 [下限, 上限] 两个整数")
    try:
        lo, hi = int(raw[0]), int(raw[1])
    except (TypeError, ValueError) as exc:
        raise StartupError(f"配置错误：pacing.{field}={raw!r} 必须是两个整数") from exc
    if lo > hi:
        raise StartupError(f"配置错误：pacing.{field} 的下限 {lo} 大于上限 {hi}")
    if lo < floor:
        raise StartupError(
            f"配置错误：pacing.{field} 的下限 {lo} 小于硬下限 {floor}。"
            f"低于这个值的节奏在行为画像上和脚本没有区别，不允许配置。"
        )
    return lo, hi


def parse_config(raw: dict[str, Any] | None, *, daily_cap: int = 0) -> PacerConfig:
    """把 `config.yaml` 的 `pacing` 段解析成 `PacerConfig`。任何不合法 → `StartupError`。"""
    block = raw or {}
    if not isinstance(block, dict):
        raise StartupError("配置错误：pacing 段必须是 mapping（键值对）")

    segments = parse_segments(block.get("segments"))

    interval_lo, interval_hi = _positive_pair(
        block.get("interval_s") or [90, 240], "interval_s", INTERVAL_FLOOR
    )
    round_min, round_max = _positive_pair(
        block.get("round") or [3, 5], "round", ROUND_MIN_HARD
    )
    if round_max > ROUND_MAX_HARD:
        raise StartupError(
            f"配置错误：pacing.round 的上限 {round_max} 超过硬上限 {ROUND_MAX_HARD}。"
            f"单轮连发超过 {ROUND_MAX_HARD} 条会让'连续动作'特征重新变明显，"
            f"这是闭环 1 要消灭的东西。"
        )

    # 轮间长休：下限允许为 0（测试与小规模验证要能关掉），但正常配置建议 15–30 分钟
    round_gap_lo, round_gap_hi = _positive_pair(
        block.get("round_gap_s") or [900, 1800], "round_gap_s", 0
    )

    ramp_raw = block.get("ramp") or []
    if not isinstance(ramp_raw, (list, tuple)) or not ramp_raw:
        raise StartupError("配置错误：pacing.ramp 必须是非空的周目标列表，例如 [15, 25, 40, 50]")
    ramp: list[int] = []
    for index, value in enumerate(ramp_raw):
        try:
            num = int(value)
        except (TypeError, ValueError) as exc:
            raise StartupError(f"配置错误：pacing.ramp 第 {index + 1} 项 {value!r} 不是整数") from exc
        if num <= 0:
            raise StartupError(f"配置错误：pacing.ramp 第 {index + 1} 项 {num} 必须为正数")
        ramp.append(num)
    step_raw = block.get("ramp_step_days", 7)
    try:
        ramp_step_days = int(step_raw)
    except (TypeError, ValueError) as exc:
        raise StartupError(f"配置错误：pacing.ramp_step_days={step_raw!r} 不是整数") from exc
    if ramp_step_days < 1:
        raise StartupError("配置错误：pacing.ramp_step_days 必须 >= 1（1 表示每天升一档）")

    if daily_cap and max(ramp) > daily_cap:
        raise StartupError(
            f"配置错误：pacing.ramp 的最大值 {max(ramp)} 超过 quota.daily_greet={daily_cap}。"
            f"爬坡目标高于日额度时，后半段永远发不出去，进度条会永远停在 100% 以下。"
            f"请把 ramp 收到 {daily_cap} 以内，或调高 daily_greet（不得超过 daily_greet_max）。"
        )

    cooldown = dict(DEFAULT_COOLDOWN_SECONDS)
    custom = block.get("cooldown_s")
    if custom is not None:
        if not isinstance(custom, dict):
            raise StartupError("配置错误：pacing.cooldown_s 必须是 mapping（键值对）")
        for key, value in custom.items():
            try:
                cooldown[str(key)] = int(value)
            except (TypeError, ValueError) as exc:
                raise StartupError(f"配置错误：pacing.cooldown_s.{key}={value!r} 不是整数") from exc

    halt_raw = block.get("halt_hours", 24)
    try:
        halt_seconds = int(halt_raw) * 3600
    except (TypeError, ValueError) as exc:
        raise StartupError(f"配置错误：pacing.halt_hours={halt_raw!r} 不是整数") from exc
    if halt_seconds <= 0:
        raise StartupError("配置错误：pacing.halt_hours 必须为正数（命中风控后必须熔断一段时间）")

    # 【2026-11-04】首次命中的冷却区间与"同日第几次才熔断"。
    # 下限给 300 秒：比一轮轮间长休还短的"冷却"等于没冷却。
    risk_lo, risk_hi = _positive_pair(
        block.get("risk_cooldown_s") or list(DEFAULT_RISK_COOLDOWN), "risk_cooldown_s", 300
    )
    try:
        halt_after = int(block.get("halt_after_risks", DEFAULT_HALT_AFTER_RISKS))
    except (TypeError, ValueError) as exc:
        raise StartupError(
            f"配置错误：pacing.halt_after_risks={block.get('halt_after_risks')!r} 不是整数"
        ) from exc
    if halt_after < 1:
        raise StartupError(
            "配置错误：pacing.halt_after_risks 必须 ≥ 1（1 = 命中一次就熔断，"
            "大于 1 = 前几次先冷却试跑）"
        )

    return PacerConfig(
        segments=segments,
        interval_lo=interval_lo,
        interval_hi=interval_hi,
        round_min=round_min,
        round_max=round_max,
        round_gap_lo=round_gap_lo,
        round_gap_hi=round_gap_hi,
        ramp=tuple(ramp),
        ramp_start=_parse_ymd(block.get("ramp_start")),
        ramp_step_days=ramp_step_days,
        cooldown=cooldown,
        halt_seconds=halt_seconds,
        risk_cooldown=(risk_lo, risk_hi),
        halt_after_risks=halt_after,
    )


# --------------------------------------------------------------------------- #
# 时段判定
# --------------------------------------------------------------------------- #
def segment_index(now: datetime, segments: Sequence[Segment]) -> int | None:
    """`now` 落在第几段（0-based）。不在任何段 → `None`。"""
    moment = now.time()
    for index, seg in enumerate(segments):
        if seg.contains(moment):
            return index
    return None


def next_window_start(now: datetime, segments: Sequence[Segment]) -> datetime | None:
    """下一次可发送窗口的开始时刻。已过期/最后一段 → 明天的第一段；无时段 → `None`。"""
    if not segments:
        return None
    today = now.date()
    for seg in segments:
        candidate = datetime.combine(today, seg.start)
        if candidate > now:
            return candidate
    return datetime.combine(today + timedelta(days=1), segments[0].start)


def next_wake_at(
    now: datetime, segments: Sequence[Segment], interval_s: int
) -> tuple[datetime, str]:
    """下一次该醒来的时间 + 原因（页面直接展示这个，用户不用猜）。

    理由只有三种：`interval`（等间隔到点）、`window`（当前不在时段，等下一段）、
    `idle`（没有配置任何时段）。
    """
    if segment_index(now, segments) is not None:
        return now + timedelta(seconds=max(interval_s, 0)), "interval"
    upcoming = next_window_start(now, segments)
    if upcoming is None:
        return now + timedelta(minutes=30), "idle"
    return upcoming, "window"


# --------------------------------------------------------------------------- #
# 爬坡与日目标
# --------------------------------------------------------------------------- #
def ramp_target(
    cfg: PacerConfig, today: date, *, daily_cap: int, start: date | None = None
) -> int:
    """今天的日目标。在第 `n` 周用 `ramp[n]`，超出列表长度就用最后一项。

    `daily_cap` 是 `quota.daily_greet`：爬坡目标再高也不许超过它，
    否则进度条的分母（额度）和实际能发的数对不上。
    """
    ramp = cfg.ramp
    begin = start or cfg.ramp_start
    if begin is None:
        index = 0
    else:
        days = (today - begin).days
        step = max(int(cfg.ramp_step_days or 7), 1)
        index = max(days, 0) // step
        if index >= len(ramp):
            index = len(ramp) - 1
    target = ramp[index]
    return min(target, daily_cap) if daily_cap else target


def segments_quota_share(target: int, segment_count: int) -> list[int]:
    """把日目标按段拆分（每段约 1/3）。余数给最后一段，保证合计等于 target。

    只用于页面展示"每段大概发几条"，pacer 不做硬性分段的强约束——
    某一段没跑完就顺延到下一段，比"卡死在 17 条"更符合真人行为。
    """
    if segment_count <= 0:
        return []
    base = target // segment_count
    parts = [base] * segment_count
    parts[-1] += target - base * segment_count
    return parts


# --------------------------------------------------------------------------- #
# 冷却与熔断
# --------------------------------------------------------------------------- #
def cooldown_for(verdict: str, cfg: PacerConfig) -> int:
    """判定码 → 该休息多少秒。`ok` / `skipped` / `error` 不休息（返回 0）。"""
    if verdict in ("ok", "skipped"):
        return 0
    if verdict == "risk":
        return int(cfg.halt_seconds)
    return int(cfg.cooldown.get(verdict, 0))


def is_halt(verdict: str) -> bool:
    """命中风控要熔断（需要人工介入 + 长冷却），与普通冷却区分开。"""
    return verdict == "risk"
