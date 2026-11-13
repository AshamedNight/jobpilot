"""`app.core.enrich` 的纯逻辑测试 —— 6 条（任务书 §11 第 1–6 条）。

这个文件**不碰数据库、不碰适配器实现**：只用两个 stub 表达"有 detail 方法"和
"capabilities 里有 detail 但没有方法"（后者是 `FakeAdapter` 的真实形状，§14 第一条）。
"""

from __future__ import annotations

from app.core import enrich
from app.core import matcher


class _DetailAdapter:
    """正常形状：有 `capabilities` 也有 `detail()`。"""

    capabilities = {"search", "detail", "chat"}

    def detail(self, security_id: str, job_id: str | None = None) -> dict:
        return {"ok": True, "job_id": job_id, "jd_text": "真 JD"}


class _CapabilityOnlyAdapter:
    """`FakeAdapter` 的形状：`capabilities` 含 `"detail"`，**却没有** `detail()` 方法。

    只判 `capabilities` 的写法在这里会 `AttributeError`，所以 L0-1 必须双守卫。
    """

    capabilities = {"search", "detail", "chat"}


def _job(job_id: str, jd_text: str = "") -> dict:
    return {"job_id": job_id, "jd_text": jd_text}


_CFG = {"min_match_score": 60}


# --------------------------------------------------------------------------- #
# 1. L0-1 / L0-2
# --------------------------------------------------------------------------- #
def test_needs_detail_only_when_jd_missing():
    """JD 空 → 该抓；JD 非空 → `already_has_jd`；没有 `detail()` 方法 → `no_capability`。"""
    adapter = _DetailAdapter()

    # JD 为空：该抓
    ok, reason = enrich.needs_detail(_job("j1", ""), adapter, 70.0, cfg=_CFG)
    assert ok is True
    assert reason is None

    # 空白字符不算正文：仍然该抓（别被 " " 骗过去）
    ok, reason = enrich.needs_detail(_job("j2", "   \n"), adapter, 70.0, cfg=_CFG)
    assert ok is True

    # JD 非空：跳过，且理由是 already_has_jd
    ok, reason = enrich.needs_detail(_job("j3", "岗位职责：审核合同"), adapter, 70.0, cfg=_CFG)
    assert ok is False
    assert reason == enrich.REASON_ALREADY_HAS_JD

    # §14 第一条：capabilities 有 "detail" 但没方法 → no_capability，**不许 AttributeError**
    ok, reason = enrich.needs_detail(_job("j4", ""), _CapabilityOnlyAdapter(), 70.0, cfg=_CFG)
    assert ok is False
    assert reason == enrich.REASON_NO_CAPABILITY

    # 适配器为 None 也不能崩
    ok, reason = enrich.needs_detail(_job("j5", ""), None, 70.0, cfg=_CFG)
    assert (ok, reason) == (False, enrich.REASON_NO_CAPABILITY)


# --------------------------------------------------------------------------- #
# 2. L0-3
# --------------------------------------------------------------------------- #
def test_pick_skips_hard_rejected():
    """硬淘汰（-999）的岗不抓：JD 只能新增淘汰与扣分，救不回 -999。"""
    adapter = _DetailAdapter()
    jobs = [_job("bad_1"), _job("bad_2"), _job("good")]
    base_scores = {"bad_1": matcher.HARD_REJECT_SCORE, "bad_2": -999.0, "good": 70.0}

    decision = enrich.pick_for_detail(jobs, base_scores, adapter, cfg=_CFG, batch_limit=10)

    assert decision["todo"] == ["good"]
    assert decision["prefiltered"] == 2
    assert decision["reasons"][enrich.REASON_HARD_REJECT] == 2
    # 剪枝不是额度问题：账必须分得清
    assert decision["skipped_by_quota"] == 0


# --------------------------------------------------------------------------- #
# 3. L0-4，且**必须跟着 PRACTICE_AREA_CAP 变**（防写死 32）
# --------------------------------------------------------------------------- #
def test_pick_skips_upper_bound_below_threshold(monkeypatch):
    """上界贴线剪枝：`base + PRACTICE_AREA_CAP < min_match_score` 才剪，且随常量变。"""
    adapter = _DetailAdapter()
    jobs = [_job("just_enough"), _job("too_low")]
    base_scores = {"just_enough": 30.0, "too_low": 20.0}

    # 默认 PRACTICE_AREA_CAP=32、阈值 60：30+32=62 ≥ 60 留；20+32=52 < 60 剪
    decision = enrich.pick_for_detail(jobs, base_scores, adapter, cfg=_CFG, batch_limit=10)
    assert decision["todo"] == ["just_enough"]
    assert decision["reasons"] == {enrich.REASON_UPPER_BOUND: 1}

    # 把常量改小：同一个 30 分岗位 30+10=40 < 60 → 也必须被剪
    # （写死 `32` 的实现会在这里红的）
    monkeypatch.setattr(matcher, "PRACTICE_AREA_CAP", 10)
    decision = enrich.pick_for_detail(jobs, base_scores, adapter, cfg=_CFG, batch_limit=10)
    assert decision["todo"] == []
    assert decision["reasons"] == {enrich.REASON_UPPER_BOUND: 2}

    # 阈值也从配置读，不写死 60：cap 仍是 10，阈值抬到 45 → 30+10=40 < 45，两条都剪
    decision = enrich.pick_for_detail(
        jobs, base_scores, adapter, cfg={"min_match_score": 45}, batch_limit=10
    )
    assert decision["todo"] == []
    assert decision["reasons"] == {enrich.REASON_UPPER_BOUND: 2}

    # 常量恢复 32、阈值 45 → 30+32=62、20+32=52 都够线，两条都该抓
    monkeypatch.setattr(matcher, "PRACTICE_AREA_CAP", 32)
    decision = enrich.pick_for_detail(
        jobs, base_scores, adapter, cfg={"min_match_score": 45}, batch_limit=10
    )
    assert decision["todo"] == ["just_enough", "too_low"]


# --------------------------------------------------------------------------- #
# 4. L1 截断 + 排序稳定 + L2 日额度（两把刀都要能切开）
# --------------------------------------------------------------------------- #
def test_pick_respects_batch_limit_and_order():
    """分数降序、同分保持传入顺序；`batch_limit` 与 `daily_remaining` 取小。"""
    adapter = _DetailAdapter()
    jobs = [_job("j1"), _job("j2"), _job("j3"), _job("j4")]
    base_scores = {"j1": 70.0, "j2": 90.0, "j3": 70.0, "j4": 80.0}

    # L1：只抓 2 条 → 最高分两条
    decision = enrich.pick_for_detail(jobs, base_scores, adapter, cfg=_CFG, batch_limit=2)
    assert decision["todo"] == ["j2", "j4"]
    assert decision["skipped_by_quota"] == 2
    assert decision["limited_by"] == "batch"
    assert decision["prefiltered"] == 0  # 额度截断的条数**不许**混进剪枝账

    # 同分稳定性：放开到 3 条 → j1 在 j3 前面（传入顺序）
    decision = enrich.pick_for_detail(jobs, base_scores, adapter, cfg=_CFG, batch_limit=3)
    assert decision["todo"] == ["j2", "j4", "j1"]

    # L2：日额度只剩 1 条 → 比 batch_limit 更紧的那把刀生效
    decision = enrich.pick_for_detail(
        jobs, base_scores, adapter, cfg=_CFG, batch_limit=10, daily_remaining=1
    )
    assert decision["todo"] == ["j2"]
    assert decision["skipped_by_quota"] == 3
    assert decision["limited_by"] == "daily"

    # 日额度已经用完 → 一条都不抓，但仍然"有账可查"
    decision = enrich.pick_for_detail(
        jobs, base_scores, adapter, cfg=_CFG, batch_limit=10, daily_remaining=0
    )
    assert decision["todo"] == []
    assert decision["skipped_by_quota"] == 4
    assert decision["limited_by"] == "daily"


# --------------------------------------------------------------------------- #
# 5. L0-5 / L0-6（注入 is_claimed / is_greeted，不碰数据库）
# --------------------------------------------------------------------------- #
def test_pick_skips_claimed_today():
    """当天已抓过 → `claimed_today`；已打过招呼 → `already_greeted`。两者都算剪枝。"""
    adapter = _DetailAdapter()
    jobs = [_job("done_today"), _job("greeted"), _job("fresh")]
    base_scores = {"done_today": 80.0, "greeted": 80.0, "fresh": 70.0}

    decision = enrich.pick_for_detail(
        jobs,
        base_scores,
        adapter,
        cfg=_CFG,
        batch_limit=10,
        is_claimed=lambda job_id: job_id == "done_today",
        is_greeted=lambda job_id: job_id == "greeted",
    )

    assert decision["todo"] == ["fresh"]
    assert decision["reasons"] == {
        enrich.REASON_CLAIMED_TODAY: 1,
        enrich.REASON_ALREADY_GREETED: 1,
    }
    assert decision["prefiltered"] == 2
    assert decision["skipped_by_quota"] == 0

    # 不注入时两者都不生效（纯逻辑层不依赖数据库）
    decision = enrich.pick_for_detail(jobs, base_scores, adapter, cfg=_CFG, batch_limit=10)
    assert decision["todo"] == ["done_today", "greeted", "fresh"]


# --------------------------------------------------------------------------- #
# 6. 合并纯函数
# --------------------------------------------------------------------------- #
def test_merge_detail_does_not_overwrite_nonempty_jd():
    """`merge_detail()`：只补空、不覆盖；不改入参；失败载荷一个字都不改。"""
    original = _job("j1", "")
    payload = {"ok": True, "job_id": "j1", "jd_text": "岗位职责：审核合同"}

    merged = enrich.merge_detail(original, payload)
    assert merged["jd_text"] == "岗位职责：审核合同"
    assert original["jd_text"] == ""  # 纯函数：入参未被修改
    assert merged is not original

    # 已有正文 → 保留原样（不能用新抓的盖掉人工补录/上一轮更好的正文）
    with_text = _job("j2", "人工补录的正文")
    merged = enrich.merge_detail(with_text, {"ok": True, "jd_text": "新抓的正文"})
    assert merged["jd_text"] == "人工补录的正文"

    # 失败载荷 / 空正文：不改一个字
    for bad in (None, {"ok": False, "jd_text": "不该用"}, {"ok": True, "jd_text": ""}):
        merged = enrich.merge_detail(_job("j3", ""), bad)
        assert merged["jd_text"] == ""
