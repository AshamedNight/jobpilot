"""节奏引擎（闭环 1）—— `app/core/pacing.py` 的纯函数 + `app/services/pacer.py` 的决策。

这一组不跑真实等待（90–240s 的间隔在这里一条都不睡），只验证**决策对不对**：

- 时段算错 → 半夜发招呼，等于自杀
- 间隔下限被绕过 → 又回到 3 秒一条
- 冷却/熔断不落盘 → 重启就"看起来正常了"，然后被平台再锁一次

真实 3 天连续运行的验收在真机上做（见 `现状盘点与推进路线-v1.md` 闭环 1 验收口径），
自动化测试只保证"参数与决策不会退化"。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest

from app import models
from app.config import StartupError
from app.core import pacing as P
from app.main import create_app
from app.services.pacer import BLOCKED_SECONDS, IDLE_SECONDS, Pacer


# --------------------------------------------------------------------------- #
# 1. 时段解析
# --------------------------------------------------------------------------- #
def test_parse_segments_sorts_and_accepts_normal_input():
    segs = P.parse_segments([["14:00", "18:00"], ["09:00", "12:00"]])
    assert [str(s) for s in segs] == ["09:00-12:00", "14:00-18:00"]
    assert isinstance(segs[0].start, time)


def test_parse_segments_rejects_overlap():
    with pytest.raises(StartupError) as exc:
        P.parse_segments([["09:00", "13:00"], ["12:00", "15:00"]])
    assert "重叠" in str(exc.value)


def test_parse_segments_rejects_cross_midnight():
    with pytest.raises(StartupError) as exc:
        P.parse_segments([["22:00", "02:00"]])
    assert "跨零点" in str(exc.value)


def test_parse_segments_rejects_bad_shape():
    with pytest.raises(StartupError):
        P.parse_segments("09:00-12:00")
    with pytest.raises(StartupError):
        P.parse_segments([["09:00"]])


def test_empty_segments_means_never_send():
    assert P.parse_segments([]) == ()
    assert P.parse_segments(None) == ()


# --------------------------------------------------------------------------- #
# 2. 参数校验：不许悄悄调快
# --------------------------------------------------------------------------- #
_BASE = {
    "segments": [["09:00", "12:00"], ["14:00", "18:00"], ["19:00", "21:30"]],
    "interval_s": [90, 240],
    "round": [3, 5],
    "ramp": [15, 25, 40, 50],
}


def test_parse_config_ok():
    cfg = P.parse_config(_BASE, daily_cap=50)
    assert cfg.interval_lo == 90 and cfg.interval_hi == 240
    assert cfg.round_min == 3 and cfg.round_max == 5
    assert cfg.halt_seconds == 24 * 3600


def test_interval_below_floor_is_rejected():
    """间隔写 3 秒（真机事故的配置）必须起不来。"""
    with pytest.raises(StartupError) as exc:
        P.parse_config({**_BASE, "interval_s": [3, 5]}, daily_cap=50)
    assert "硬下限" in str(exc.value)


def test_round_above_hard_limit_is_rejected():
    with pytest.raises(StartupError) as exc:
        P.parse_config({**_BASE, "round": [3, 20]}, daily_cap=50)
    assert "硬上限" in str(exc.value)


def test_ramp_above_daily_cap_is_rejected():
    with pytest.raises(StartupError) as exc:
        P.parse_config(_BASE, daily_cap=5)
    assert "超过 quota.daily_greet" in str(exc.value)


def test_ramp_must_be_non_empty_positive():
    with pytest.raises(StartupError):
        P.parse_config({**_BASE, "ramp": []}, daily_cap=50)
    with pytest.raises(StartupError):
        P.parse_config({**_BASE, "ramp": [0, 10]}, daily_cap=50)


def test_lo_greater_than_hi_rejected():
    with pytest.raises(StartupError):
        P.parse_config({**_BASE, "interval_s": [240, 90]}, daily_cap=50)


# --------------------------------------------------------------------------- #
# 3. 时段定位
# --------------------------------------------------------------------------- #
@pytest.fixture()
def cfg():
    return P.parse_config(_BASE, daily_cap=50)


def test_segment_index_inside_and_outside(cfg):
    assert P.segment_index(datetime(2026, 11, 3, 9, 0), cfg.segments) == 0
    assert P.segment_index(datetime(2026, 11, 3, 11, 59), cfg.segments) == 0
    assert P.segment_index(datetime(2026, 11, 3, 12, 0), cfg.segments) is None  # 右端点不含
    assert P.segment_index(datetime(2026, 11, 3, 13, 0), cfg.segments) is None
    assert P.segment_index(datetime(2026, 11, 3, 3, 0), cfg.segments) is None


def test_next_window_start_same_day_and_tomorrow(cfg):
    now = datetime(2026, 11, 3, 13, 0)
    assert P.next_window_start(now, cfg.segments) == datetime(2026, 11, 3, 14, 0)
    late = datetime(2026, 11, 3, 23, 30)
    assert P.next_window_start(late, cfg.segments) == datetime(2026, 11, 4, 9, 0)


def test_next_wake_at_reasons(cfg):
    inside = datetime(2026, 11, 3, 10, 0)
    moment, reason = P.next_wake_at(inside, cfg.segments, 120)
    assert reason == "interval"
    assert moment == inside + timedelta(seconds=120)

    outside = datetime(2026, 11, 3, 13, 0)
    moment, reason = P.next_wake_at(outside, cfg.segments, 120)
    assert reason == "window"
    assert moment == datetime(2026, 11, 3, 14, 0)

    moment, reason = P.next_wake_at(inside, (), 120)
    assert reason == "idle"


# --------------------------------------------------------------------------- #
# 4. 爬坡与配额拆分
# --------------------------------------------------------------------------- #
def test_ramp_target_walks_week_by_week(cfg):
    start = date(2026, 11, 4)
    assert P.ramp_target(cfg, date(2026, 11, 4), daily_cap=50, start=start) == 15
    assert P.ramp_target(cfg, date(2026, 11, 10), daily_cap=50, start=start) == 15  # 第 1 周末
    assert P.ramp_target(cfg, date(2026, 11, 11), daily_cap=50, start=start) == 25  # 第 2 周
    assert P.ramp_target(cfg, date(2026, 11, 18), daily_cap=50, start=start) == 40  # 第 3 周
    assert P.ramp_target(cfg, date(2026, 11, 25), daily_cap=50, start=start) == 50  # 第 4 周
    assert P.ramp_target(cfg, date(2026, 12, 30), daily_cap=50, start=start) == 50  # 封顶


def test_ramp_target_before_start_uses_first_rung(cfg):
    assert P.ramp_target(cfg, date(2026, 1, 1), daily_cap=50, start=date(2026, 11, 4)) == 15


def test_ramp_target_never_exceeds_cap(cfg):
    """daily_cap 比 ramp 顶更低时，以 cap 为准（页面分母是额度）。"""
    assert P.ramp_target(cfg, date(2026, 12, 30), daily_cap=30, start=date(2026, 11, 4)) == 30


def test_segments_quota_share_sums_to_target():
    for target in (0, 1, 2, 5, 17, 50):
        parts = P.segments_quota_share(target, 3)
        assert sum(parts) == target
        assert len(parts) == 3
    assert P.segments_quota_share(5, 0) == []


# --------------------------------------------------------------------------- #
# 5. 冷却 / 熔断
# --------------------------------------------------------------------------- #
def test_cooldown_mapping(cfg):
    assert P.cooldown_for("ok", cfg) == 0
    assert P.cooldown_for("skipped", cfg) == 0
    assert P.cooldown_for("limit", cfg) == 45 * 60
    assert P.cooldown_for("unconfirmed", cfg) == 10 * 60
    assert P.cooldown_for("auth", cfg) == 60 * 60
    assert P.cooldown_for("risk", cfg) == 24 * 3600


def test_is_halt_only_for_risk(cfg):
    assert P.is_halt("risk") is True
    for verdict in ("ok", "skipped", "limit", "auth", "unconfirmed", "error"):
        assert P.is_halt(verdict) is False


# --------------------------------------------------------------------------- #
# 6. Pacer：决策与落盘（不 sleep）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def app(tmp_path):
    db = tmp_path / "pacer-test.db"
    models.init_db(db).close()
    application = create_app(db_path=db)
    # 时段清空 = 永不自动发送：本组只测决策，不许碰适配器
    application.state.pacer._pcfg = replace(application.state.pacer._pcfg, segments=())
    return application


def test_pacer_built_and_disabled_by_default(app):
    pacer = app.state.pacer
    status = pacer.status()
    assert status["enabled"] is True
    assert status["running"] is False
    assert status["pool_size"] >= 20


def test_absorb_ok_returns_interval(app):
    pacer = app.state.pacer
    now = datetime(2026, 11, 3, 10, 0)
    delay = pacer._absorb({"ok": True, "verdict": "ok", "stats": {"sent_new": 1}}, now)
    assert 90 <= delay <= 240, "正常发出后应该按 90–240s 的间隔等"
    assert pacer.last_sent_at == now
    assert pacer.last_verdict == "ok"


def test_absorb_first_risk_cools_down_not_halts(app):
    """【2026-11-04 口径变更】首次命中风控只冷却 30–60 分钟，不躺一天。

    依据：社区排障口径（`drydraft/boss-recruit-kit`）对 code 37 的修复是
    "冷却 30–60 分钟后小批量试跑"，而不是直接停 24 小时。
    一次偶发就锁死一天，会把三天验证窗口整个扔掉。
    """
    pacer = app.state.pacer
    now = datetime(2026, 11, 3, 10, 0)
    delay = pacer._absorb(
        {"ok": False, "verdict": "risk", "error": "命中风控", "stats": {"sent_new": 0}}, now
    )
    assert 30 * 60 <= delay <= 60 * 60, f"首次命中应冷却 30–60 分钟，实际 {delay}"
    assert pacer.halted_until is None
    assert pacer.cooldown_until == now + timedelta(seconds=delay)
    assert pacer.status()["risk_hits_today"] == 1


def test_absorb_second_risk_same_day_halts_for_a_day(app):
    """同日第二次命中 → 24h 熔断（偶发解释不通了）。"""
    pacer = app.state.pacer
    now = datetime(2026, 11, 3, 10, 0)
    payload = {"ok": False, "verdict": "risk", "error": "命中风控", "stats": {"sent_new": 0}}
    pacer._absorb(payload, now)
    second = pacer._absorb(payload, now + timedelta(minutes=40))

    assert second == 24 * 3600
    assert pacer.halted_until == now + timedelta(minutes=40, seconds=24 * 3600)
    assert pacer.cooldown_until is None
    assert pacer.status()["risk_hits_today"] == 2


def test_absorb_auth_stops_instead_of_retrying(app):
    """【2026-11-08 事故】登录态失效必须停，不能"冷却一小时再试"。

    现场：09:00 与 10:53 两次 `AUTH_EXPIRED`（中间只隔了一次 1 小时冷却），
    14:00 就变成 `ENVIRONMENT_RISK` + 平台写回锁 + 24h 熔断。
    会话失效后重试永远不会成功，每次重试都是"拿过期凭证敲平台"的痕迹。
    """
    pacer = app.state.pacer
    now = datetime(2026, 11, 8, 9, 0)
    delay = pacer._absorb(
        {"ok": False, "verdict": "auth", "error": "greet 认证失败", "stats": {}}, now
    )

    assert pacer.running is False, "auth 必须把循环停掉，不许自己重试"
    assert pacer.last_verdict == "auth"
    assert pacer.status()["next_reason"] == "auth"
    assert "扫码" in (pacer.last_error or ""), "必须告诉人下一步做什么"
    assert pacer.halted_until is None, "登录问题不该占用 24h 熔断（那是风控用的）"
    assert delay == 24 * 3600, "返回大延时，防止外层再排一次"


def test_absorb_limit_cools_down(app):
    pacer = app.state.pacer
    now = datetime(2026, 11, 3, 10, 0)
    delay = pacer._absorb({"ok": False, "verdict": "limit", "stats": {}}, now)
    assert delay == 45 * 60
    assert pacer.cooldown_until == now + timedelta(minutes=45)


def test_absorb_skipped_waits_round_gap(app):
    pacer = app.state.pacer
    delay = pacer._absorb({"ok": True, "verdict": "skipped", "stats": {}}, datetime(2026, 11, 3, 10, 0))
    assert delay >= BLOCKED_SECONDS, "没有可发岗位时不要空转"


def test_round_size_and_interval_stay_in_range(app):
    cfg = app.state.pacer._pcfg
    import random

    rng = random.Random(7)
    for _ in range(200):
        assert 3 <= cfg.round_size(rng) <= 5
        assert 90 <= cfg.next_interval(rng) <= 240


def test_halt_state_survives_restart(app, tmp_path):
    """熔断时间必须落盘：重启服务不许提前解除（改造前风控锁只在内存里）。"""
    pacer = app.state.pacer
    pacer._halt(datetime(2026, 11, 3, 10, 0), reason="测试熔断")

    raw = json.loads(pacer._state_path.read_text(encoding="utf-8"))
    assert raw["halted_until"] == pacer.halted_until.isoformat(timespec="seconds")
    assert pacer.halted_until == datetime(2026, 11, 4, 10, 0)  # 10:00 + 24h

    fresh = Pacer(app)
    assert fresh.halted_until == pacer.halted_until
    assert fresh.last_error == "测试熔断"


def test_tick_paused_returns_blocked(app):
    # 这三条路径都不碰适配器（不走 asyncio.to_thread），所以可以直接 asyncio.run
    pacer = app.state.pacer
    app.state.paused = True
    assert asyncio.run(pacer._tick()) == BLOCKED_SECONDS
    assert pacer._next_reason == "paused"


def test_tick_risk_locked_halts(app):
    pacer = app.state.pacer
    app.state.paused = False
    app.state.risk_locked = True
    delay = asyncio.run(pacer._tick())
    assert delay == 24 * 3600
    assert pacer.halted_until is not None


def test_tick_outside_segments_waits(app):
    """时段清空（= 永不发送）时只等，不碰适配器。"""
    pacer = app.state.pacer
    app.state.paused = False
    app.state.risk_locked = False
    delay = asyncio.run(pacer._tick())
    assert delay == IDLE_SECONDS
    assert pacer._next_reason == "idle"


# --------------------------------------------------------------------------- #
# 7. API
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


def test_status_endpoint_exposes_rhythm(client):
    data = client.get("/api/pacing/status").json()
    for key in ("enabled", "running", "used", "target", "segments", "interval_s", "round", "pool_size"):
        assert key in data, f"状态里缺 {key}"

    # 【2026-11-14】原来这里写死 `interval_s == [90, 240]` / `round == [3, 5]`。
    # 节奏是**每天可能要调的经营参数**（11-14 就把 round 从 [3,5] 改成 [4,4]
    # 去对齐 send_batch），写死会让「正常调参」变成「测试变红」——
    # 红灯一旦变成噪音，真出问题时没人看了。
    # 所以这里只断言**结构与合法性边界**：两项、有序、不超过启动校验的上限。
    interval = data["interval_s"]
    assert isinstance(interval, list) and len(interval) == 2
    assert 0 < interval[0] <= interval[1]

    rnd = data["round"]
    assert isinstance(rnd, list) and len(rnd) == 2
    assert 0 < rnd[0] <= rnd[1] <= 8, "round 上限超过 8 启动就报错（见 parse_config 校验）"


def test_stop_is_idempotent(client):
    first = client.post("/api/pacing/stop").json()
    second = client.post("/api/pacing/stop").json()
    assert first["ok"] is True and second["ok"] is True
    assert client.get("/api/pacing/status").json()["running"] is False


# --------------------------------------------------------------------------- #
# 8. 单条驱动：`limit` 是 pacer 与 HTTP 共用同一段代码的关键
# --------------------------------------------------------------------------- #
def test_limit_one_sends_at_most_one(app):
    """`limit=1` 时一轮最多发一条——这是"单条驱动"的地基。"""
    from app.api.direct_greet import run_direct_greet_pass
    from app.services.pacer import _ShimRequest

    app.state.config["quota"]["daily_greet"] = 5
    conn = models.connect(app.state.db_path)
    try:
        result = run_direct_greet_pass(_ShimRequest(app), conn, app.state.config, limit=1)
        claimed = conn.execute(
            "SELECT COUNT(*) AS n FROM actions WHERE action_type='greet'"
        ).fetchone()["n"]
    finally:
        conn.close()

    assert result["ok"] is True, result
    assert result["stats"]["sent_new"] <= 1
    assert claimed <= 1
    assert result["verdict"] in ("ok", "skipped")


def test_direct_greet_uses_pool_text_not_a_single_sentence(app):
    """连发多条时，**发出去的文本**不许重复（固定文案已被证明是风控诱因）。

    注意断言的是文本唯一、不是模板 id 唯一：同一模板配不同岗位名可以渲染出
    不同的句子，这在行为上是安全的（HR 之间不共享收件箱）；
    真正要禁的是"两个 HR 收到一字不差的同一段话"。
    """
    from app.api.direct_greet import run_direct_greet_pass
    from app.services.pacer import _ShimRequest

    app.state.config["quota"]["daily_greet"] = 4
    conn = models.connect(app.state.db_path)
    try:
        run_direct_greet_pass(_ShimRequest(app), conn, app.state.config, limit=4)
        rows = conn.execute(
            "SELECT response_json FROM actions WHERE action_type='greet' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    ids = [json.loads(r["response_json"]).get("tpl") for r in rows]
    texts = list(app.state.adapter._greeting_text.values())

    assert texts, "一条都没发出去，测试前提不成立"
    assert all(ids), f"每条都该记下模板 id：{ids}"
    assert len(set(texts)) == len(texts), f"同一轮出现了重复文案：{texts}"
    assert len(set(ids)) >= 3, f"4 条只用了 {len(set(ids))} 个模板，多样性不够：{ids}"
