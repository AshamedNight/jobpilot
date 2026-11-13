"""请求形状改造：一次搜索领多条（2026-11-13）。

## 改的是什么

原来 pacer 每发一条都要重新加载一次完整列表页（上游单页 67–177 秒），
50 条 = 50 次重载。平台看到的是"同一个页面每 2–4 分钟被重载一次，
每次都立刻调一次沟通接口" —— 这是最不像人的形状。

现在：一次搜索领 `send_batch` 条，**批内保留 interval 间隔**。
省掉的是"重载列表"，不是"发送间隔" —— 这条区别是这次改造的全部安全性所在。

## 为什么这些测试重要

如果 `send_gap` 失效，改造就变成"N 条连发"，比原来危险得多；
如果 `_round_left` 不按实际发送数递减，轮次边界会往后拖 N 倍，该有的长休息永远等不到。
"""

from __future__ import annotations

from datetime import datetime

from app import models
from app.api.direct_greet import run_direct_greet_pass
from app.services.pacer import Pacer, _ShimRequest

from test_direct_greet import _DirectStub, _job, _use, offline  # noqa: F401 - fixture


def _five_jobs():
    """用剧本里**真实存在**的 5 条岗位。

    为什么不能自己编 job_id：发送成功后要做"会话反查"（`_uid_of_job`），
    它依赖桩的 `chat()` 返回的会话列表；编造的 id 不会出现在里面，
    反查会抛 AttributeError，整个 pass 被判成异常 —— 那是测试数据的问题，不是被测代码的问题。
    """
    return [_job(f"job_00{i}") for i in range(1, 6)]


def _run(client, **kwargs):
    app = client.app
    conn = models.connect(app.state.db_path)
    try:
        return run_direct_greet_pass(
            _ShimRequest(app), conn, app.state.config, page=1, url="https://example/jobs", **kwargs
        )
    finally:
        conn.close()


def test_batch_uses_one_search_for_many_sends(offline):
    client, *_ = offline
    stub = _use(client, _DirectStub(plan=_five_jobs()))
    client.app.state.config["quota"]["daily_greet"] = 50

    result = _run(client, limit=3, send_gap=(0, 0))

    assert result["ok"] is True, result
    assert result["stats"]["sent_new"] == 3, result["stats"]
    searches = [c for c in stub.calls if str(c).startswith("search")]
    assert len(searches) == 1, f"一批只该搜一次，实际 {searches}"
    assert len(stub.greet_calls) == 3


def test_batch_of_one_still_works(offline):
    """send_batch=1 必须与旧行为完全一致（可回滚是这次改造的安全阀）。"""
    client, *_ = offline
    stub = _use(client, _DirectStub(plan=_five_jobs()))
    client.app.state.config["quota"]["daily_greet"] = 50

    result = _run(client, limit=1, send_gap=(0, 0))

    assert result["stats"]["sent_new"] == 1
    assert len([c for c in stub.calls if str(c).startswith("search")]) == 1


def test_send_gap_sleeps_between_sends_only(offline, monkeypatch):
    """第 1 条立刻发，第 2/3 条之间才等待（真人点开就沟通，不会先等 2 分钟）。"""
    client, *_ = offline
    _use(client, _DirectStub(plan=_five_jobs()))
    client.app.state.config["quota"]["daily_greet"] = 50

    calls: list[float] = []

    def fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr("app.api.direct_greet.time.sleep", fake_sleep)
    _run(client, limit=3, send_gap=(90, 240))

    assert len(calls) == 2, f"发 3 条应该只等 2 次，实际 {calls}"
    assert all(90 <= s <= 240 for s in calls), calls


def _pacer(cfg: dict | None = None) -> Pacer:
    from app.core import pacing as pacing_mod

    pacer = Pacer.__new__(Pacer)
    pacer._pcfg = pacing_mod.parse_config(
        {
            "segments": [["09:00", "12:00"], ["14:00", "18:00"]],
            "interval_s": [90, 240],
            "round": [3, 5],
            "round_gap_s": [900, 1800],
            "ramp": [25, 50],
            "ramp_start": "2026-11-13",
        },
        daily_cap=50,
    )
    pacer.cfg = {"pacing": cfg or {}, "quota": {"daily_greet": 25, "daily_greet_max": 50}}
    pacer._cap_today = None
    pacer._cap_day = None
    pacer._extended_target = None
    pacer._extended_day = None
    pacer._save_state = lambda: None   # 单测不需要落盘
    return pacer


def test_extend_target_adds_a_step_when_time_left():
    pacer = _pacer({"send_batch": 4, "extend_step": 5})
    # 15:30 距 18:00 还有 150 分钟，足够加
    got = pacer._extend_target(datetime(2026, 11, 13, 15, 30), 25)
    assert got == 30


def test_extend_target_does_nothing_near_segment_end():
    pacer = _pacer({"send_batch": 4, "extend_step": 5})
    got = pacer._extend_target(datetime(2026, 11, 13, 17, 59), 25)
    assert got == 25, "只剩 1 分钟就别加了"


def test_extend_target_never_exceeds_hard_max():
    pacer = _pacer({"send_batch": 4, "extend_step": 50})
    got = pacer._extend_target(datetime(2026, 11, 13, 10, 0), 25)
    assert got == 50, "不能被 extend_step 顶穿 daily_greet_max"


def test_extend_is_remembered_so_it_does_not_unlock_every_round():
    """加量结果必须被记住。

    2026-11-13 现场：第一版没落盘，于是每轮 `_target_today()` 都还是 25，
    `used >= target` 立刻成立 → 每轮又加一档，一个晚上能从 25 一路发到 50。
    """
    pacer = _pacer({"send_batch": 4, "extend_step": 5})
    now = datetime(2026, 11, 13, 15, 30)

    first = pacer._extend_target(now, 25)
    assert first == 30
    assert pacer._target_today() == 30, "目标必须立刻反映加量结果"

    # 再跑一轮（模拟已发满 30 之后）：才允许再加一档
    second = pacer._extend_target(now, pacer._target_today())
    assert second == 35


def test_extend_is_remembered_so_it_does_not_unlock_every_round():
    """加量结果必须被记住。

    2026-11-13 现场：第一版没落盘，于是每轮 `_target_today()` 都还是 25，
    `used >= target` 立刻成立 → 每轮又加一档，一个晚上能从 25 一路发到 50。
    """
    pacer = _pacer({"send_batch": 4, "extend_step": 5})
    now = datetime(2026, 11, 13, 15, 30)

    first = pacer._extend_target(now, 25)
    assert first == 30
    assert pacer._target_today() == 30, "目标必须立刻反映加量结果"

    # 再跑一轮（模拟已发满 30 之后）：才允许再加一档
    second = pacer._extend_target(now, pacer._target_today())
    assert second == 35
