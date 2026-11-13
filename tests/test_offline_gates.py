"""离线收口 B-3 / B-4 / B-6 —— 4 条（纯离线：临时库 + 本地桩，零 CLI、零网络）。

这三条验收项在 B 组真机上被风控挡住了，但它们**根本不需要上游**：
`claimed_today` 就是 `actions` 表的行，`count_actions_today()` 就是按 `action_type` 数行，
`idem.try_claim()` 就是 `idempotency_key` 的 UNIQUE。所以完全可以在本地把闸门逐个掰开。

桩数据是固定集合（`FAKE_JOBS` 8 条，桩的 `search` 一律返回 `jd_text=""`，正文只有
`detail()` 给得出），因此下面每个数字都是可手算的：

| 岗位 | 无 JD 预打分 | detail 后 | 结论 |
|---|---|---|---|
| job_001 / 002 / 003 / 008 | 50 | 82 / 74 / 74 / 74 | **detail 候选 4 条，且全部过线** |
| job_004 / 005 / 007 | -999 | -999 | 硬淘汰，L0-3 剪枝 |
| job_006 | 0 | 0 | 上界 0+32=32 < 60，L0-4 剪枝 |

⇒ 每轮 `enrich_prefiltered = 4`（3 硬淘汰 + 1 上界），`detail` 候选恒为 4 条。

三道护栏沿用 `test_offline_guarantee.py` 的口径（CLI 探针 / 网络探针 / 真适配器探针），
本文件自带一份，保证单独读这一份也能看懂"凭什么说它没出网"。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

#: 与 `test_offline_guarantee.py` 共用同一份探针实现（原来是各抄一份）
from offline_probe import SocketTripwire, Tripwire, offline_stack

from app import models
from app.adapters.fake import JOBS as FAKE_JOBS
from app.adapters.fake import FakeAdapter
from app.core import enrich
from app.main import create_app

#: 固定岗位集合（桩 `search` 永远返回这 4 条可 detail 的岗位 + 4 条必被剪枝的）
CANDIDATE_JOBS = ("job_001", "job_002", "job_003", "job_008")
PRUNED_JOBS = ("job_004", "job_005", "job_006", "job_007")


class _StubAdapter(FakeAdapter):
    """本地桩：`search` 不带 JD，`detail` 给正文，`greet` 记账并成功。"""

    def __init__(self) -> None:
        super().__init__()
        self._jd = {str(job["job_id"]): str(job.get("jd_text") or "") for job in FAKE_JOBS}
        self.detail_calls: list[str] = []
        self.greet_calls: list[str] = []

    def probe(self) -> set[str]:
        self.calls.append("probe")
        return set(self.capabilities)

    def search(self, query: str = "", **filters) -> list[dict]:
        self.calls.append(f"search:{query}")
        return [dict(job, jd_text="") for job in self._jobs]

    def detail(self, security_id: str, job_id: str | None = None) -> dict:
        self.detail_calls.append(str(job_id))
        return {
            "ok": True,
            "job_id": str(job_id or ""),
            "jd_text": self._jd.get(str(job_id), ""),
            "address": "",
            "hr_active": "3d",
            "greeted": False,
        }

    def greet(self, security_id: str, job_id: str, message: str) -> dict:
        self.greet_calls.append(str(job_id))
        return {
            "sent": True,
            "error_code": None,
            "detail": "桩：打招呼成功",
            "raw": {"code": 0},
            "already_greeted": False,
        }


@pytest.fixture()
def offline(tmp_path: Path, monkeypatch):
    """临时库 + 三只探针；同一个 fixture 实例 = 同一个库（多轮就跑在它上面）。

    socket 探针由 `offline_stack` 管生命周期（审查条目三）：进 TestClient 之后才装、
    退 TestClient 之前必在 `finally` 里恢复。
    """
    import app.adapters.boss as boss_module
    import app.main as main_module

    cli = Tripwire("真实 Boss CLI 入口 boss._cli_entry")
    sock = SocketTripwire()
    boss_cls = Tripwire("真适配器类 app.main.BossAdapter")

    monkeypatch.setattr(boss_module, "_cli_entry", cli)
    monkeypatch.setattr(main_module, "BossAdapter", boss_cls)

    app = create_app(db_path=tmp_path / "gates.db", env_path=tmp_path / "no.env")
    with offline_stack(app, sock=sock) as client:
        yield client, cli, sock, boss_cls


def _assert_offline(cli, sock, boss_cls) -> None:
    assert cli.hits == [], f"调用了真实 Boss CLI：{cli.hits}"
    assert sock.hits == [], f"发生了网络连接：{sock.hits}"
    assert boss_cls.hits == [], f"构造了真适配器：{boss_cls.hits}"


def _detail_ledger(client) -> dict[str, str]:
    """`actions` 里今天的 detail 键 → 状态。"""
    conn = models.connect(client.app.state.db_path)
    try:
        return {
            row["idempotency_key"]: row["status"]
            for row in conn.execute(
                "SELECT idempotency_key, status FROM actions WHERE action_type = 'detail'"
            )
        }
    finally:
        conn.close()


def _greet_keys(client) -> set[str]:
    conn = models.connect(client.app.state.db_path)
    try:
        return {
            row["idempotency_key"]
            for row in conn.execute("SELECT idempotency_key FROM actions WHERE action_type = 'greet'")
        }
    finally:
        conn.close()


def _stored(client) -> dict[str, tuple]:
    """落库的 (jd_text, match_score)，按 job_id —— 用于证明第二轮"不改写、不丢正文"。"""
    conn = models.connect(client.app.state.db_path)
    try:
        rows = {
            job_id: models.get_job(conn, job_id) or {}
            for job_id in ("job_001", "job_002", "job_003", "job_008")
        }
    finally:
        conn.close()
    return {
        job_id: (str(row.get("jd_text") or ""), row.get("match_score"))
        for job_id, row in rows.items()
    }


def _sweep(client) -> dict:
    result = client.post("/api/run/sweep").json()
    assert result["ok"] is True, result
    return result["stats"]


# --------------------------------------------------------------------------- #
# B-3（§12 B-3）：同日重跑 → 对应岗位被当日 claim 剪枝，不重复 detail
# --------------------------------------------------------------------------- #
def test_b3_same_day_claim_prunes_detail_without_quota_pressure(offline):
    """同一临时库连续两轮：第一轮真抓 4 条，第二轮**一条都不重抓**。

    额度必须充足，否则"第二轮没抓"会被误读成"额度挡住了"：
    `daily_detail=50` 而两轮合计最多 8 条，`detail_batch=10` 也远大于 4。
    """
    client, cli, sock, boss_cls = offline
    stub = _StubAdapter()
    client.app.state.adapter = stub
    client.app.state.adapter_name = "gate_stub"

    quota = client.app.state.config["quota"]
    assert quota["daily_detail"] == 50, "日额度必须充足（默认 50），排除额度原因"
    assert quota["detail_batch"] == 10, "单轮额度必须充足（默认 10 > 4 候选），排除额度原因"
    # **把 greet 侧彻底静默**：否则第二轮那 4 条候选会同时被 L0-5（当日已抓）和
    # L0-6（已打过招呼）两道判据剪掉，`enrich_prefiltered == 8` 就同时成立两次，
    # 这条测试便分不清是哪一条在起作用（实测：只关 L0-5，它仍然全绿）。
    # `daily_greet=0` 让第一轮不发招呼 → 第二轮没有 greet 键 → L0-6 掉，
    # 只剩 "当日 claim" 这一条判据能解释 prefiltered 从 4 涨到 8。
    quota["daily_greet"] = 0

    # ---- 第一轮：4 条候选全部真抓 ----
    first = _sweep(client)
    assert first["enriched"] == 4
    assert sorted(stub.detail_calls) == sorted(CANDIDATE_JOBS)
    assert first["skipped_by_detail_quota"] == 0, "第一轮没被额度截断"
    assert first["enrich_prefiltered"] == 4, "4 条剪枝 = 3 硬淘汰 + 1 上界"
    assert first["passed"] == 4
    # greet 侧静默（daily_greet=0）：过线但不发，且不留 greet 键（隔离 L0-6 用）
    assert first["greeted"] == 0
    assert _greet_keys(client) == set()

    stored_after_first = _stored(client)
    assert all(jd_text for jd_text, _score in stored_after_first.values()), "第一轮正文必须已落库"

    ledger_after_first = _detail_ledger(client)
    assert sorted(ledger_after_first) == sorted(
        [f"detail:{job_id}:{datetime.now(timezone.utc).strftime('%Y%m%d')}" for job_id in CANDIDATE_JOBS]
    )
    assert set(ledger_after_first.values()) == {"ok"}, "第一轮 4 条 claim 全 ok"

    status_after_first = client.get("/api/status").json()
    assert status_after_first["quota"]["used_detail_today"] == 4
    # 日额度仍然剩很多（46 条），第二轮"没抓"绝不可能是额度导致
    assert quota["daily_detail"] - status_after_first["quota"]["used_detail_today"] == 46

    # ---- 第二轮：同一库，候选全被当日 claim 剪枝 ----
    second = _sweep(client)
    assert second["enriched"] == 0, "第二轮不许再抓"
    assert stub.detail_calls == list(stub.detail_calls[:4]), "detail 调用记录一条都不许增加"
    assert len(stub.detail_calls) == 4, f"detail 总共只该调 4 次，实际 {stub.detail_calls}"
    assert second["skipped_by_detail_quota"] == 0, "不是额度挡的（额度充足）"
    # 剪枝数从 4 涨到 8，差额 4 就是**当日 claim** 的数量
    assert second["enrich_prefiltered"] == 8
    assert second["enrich_prefiltered"] - first["enrich_prefiltered"] == 4

    # 账本不增行、状态不变
    ledger_after_second = _detail_ledger(client)
    assert ledger_after_second == ledger_after_first, "detail 账本与第一轮完全一致"

    # 第二轮 search 仍返回无 JD 的列表项，但库里已有正文 ⇒ **过线数必须仍是 4**。
    # 【跨轮 JD 评分修复 · 2026-10-28】fix6 及之前这里是 0，原因是
    # `matcher.score(job, cfg)` 用的是**本轮空正文的搜索项**，回读的 `stored.jd_text`
    # 只进了 `gap_flags` 与落库。去重（不重抓 detail）与评分（复用已存正文）是两件事，
    # 不能因为前者正确就给 0 找解释。本条为修复清单 §3 第 2 条授权的断言收敛。
    assert second["passed"] == 4
    assert second["greeted"] == 0
    # 正文与分数一个字都不许变（复用已存 JD 的直接证据，不是靠 passed 一个数字）
    assert _stored(client) == stored_after_first, "第二轮不许改写正文与分数"

    _assert_offline(cli, sock, boss_cls)


# --------------------------------------------------------------------------- #
# B-4a（§12 B-4）：单轮额度 `detail_batch`
# --------------------------------------------------------------------------- #
def test_b4a_single_round_detail_batch_truncates(offline):
    """`detail_batch=2`、日额度充足 → 只抓 2 条，另 2 条记进 `skipped_by_detail_quota`。"""
    client, cli, sock, boss_cls = offline
    stub = _StubAdapter()
    client.app.state.adapter = stub
    client.app.state.adapter_name = "gate_stub"
    client.app.state.config["quota"]["detail_batch"] = 2
    client.app.state.config["quota"]["daily_detail"] = 50

    stats = _sweep(client)

    assert stats["enriched"] == 2, "单轮上限必须真的截断到 2"
    assert stats["skipped_by_detail_quota"] == 2, "被单轮上限截掉的 2 条必须有账"
    assert len(stub.detail_calls) == 2, "适配器只该被调 2 次"
    # 分高者先抓：001(82) / 002(74) 排在 003(74) / 008(74) 之前（同分按传入顺序）
    assert sorted(stub.detail_calls) == ["job_001", "job_002"]
    assert stats["enrich_prefiltered"] == 4, "剪枝账与额度账必须分开（剪枝仍是 4）"

    # 账本：只有 2 行 detail
    ledger = _detail_ledger(client)
    assert len(ledger) == 2
    assert set(ledger.values()) == {"ok"}

    status = client.get("/api/status").json()
    assert status["quota"]["used_detail_today"] == 2
    assert status["quota"]["detail_batch"] == 2
    assert status["quota"]["daily_detail"] == 50

    _assert_offline(cli, sock, boss_cls)


# --------------------------------------------------------------------------- #
# B-4b（§12 B-4）：日额度 `daily_detail`
# --------------------------------------------------------------------------- #
def test_b4b_daily_detail_quota_blocks_eligible_candidates(offline):
    """日额度耗尽 → 第二轮**仍有未 claim 且符合 detail 条件的候选**，但一条都不抓。

    这是 B-4 里最容易做假的一条：如果第二轮本来就没候选，`enriched=0` 毫无意义。
    所以这里显式证明"候选存在但被日额度挡住"——
    用 `enrich.pick_for_detail(daily_remaining=None)` 把同一批数据再挑一遍，
    挑得出来 2 条，而 sweep 一条都没调。
    """
    client, cli, sock, boss_cls = offline
    stub = _StubAdapter()
    client.app.state.adapter = stub
    client.app.state.adapter_name = "gate_stub"
    # 日额度只给 2 条；单轮给 10（比日额度宽松，确保是**日额度**这把刀切的）
    client.app.state.config["quota"]["daily_detail"] = 2
    client.app.state.config["quota"]["detail_batch"] = 10

    # ---- 第一轮：用掉全部日额度 ----
    first = _sweep(client)
    assert first["enriched"] == 2
    assert len(stub.detail_calls) == 2
    status_first = client.get("/api/status").json()
    assert status_first["quota"]["used_detail_today"] == 2
    assert status_first["quota"]["daily_detail"] == 2, "当日额度已耗尽"
    assert status_first["quota"]["used_detail_today"] == status_first["quota"]["daily_detail"]

    claimed_first = set(stub.detail_calls)

    # ---- 中间证明：确实还剩 2 条"未 claim + 符合 detail 条件"的候选 ----
    cfg = client.app.state.config
    conn = models.connect(client.app.state.db_path)
    try:
        stored = [models.get_job(conn, job_id) for job_id in CANDIDATE_JOBS]
    finally:
        conn.close()
    blank_jobs = [dict(job, jd_text="") for job in stored]
    from app.core import matcher

    base_scores = {str(job["job_id"]): matcher.score(job, cfg)[0] for job in blank_jobs}
    dry_run = enrich.pick_for_detail(
        blank_jobs,
        base_scores,
        stub,
        cfg=cfg,
        batch_limit=10,
        daily_remaining=None,  # 不施加日额度，只问"逻辑上还该抓谁"
        is_claimed=lambda job_id: job_id in claimed_first,
        is_greeted=lambda job_id: False,
    )
    remaining_candidates = sorted(dry_run["todo"])
    assert remaining_candidates == ["job_003", "job_008"], (
        f"第二轮必须有未 claim、符合 detail 条件的候选，实际 {remaining_candidates}"
    )
    assert dry_run["skipped_by_quota"] == 0, "逻辑上它们都该抓（还没被额度切）"

    # ---- 第二轮：同样的候选，但当日额度耗尽 → 一条都不调 ----
    second = _sweep(client)
    assert second["enriched"] == 0, "日额度耗尽后不许再抓"
    assert len(stub.detail_calls) == 2, f"detail 调用数不许增加，实际 {stub.detail_calls}"
    assert second["skipped_by_detail_quota"] == 2, "被日额度挡住的 2 条必须有账"
    # 剪枝 4（硬淘汰/上界）+ 2（当日 claim）= 6
    assert second["enrich_prefiltered"] == 6
    assert second["enriched_failed"] == 0, "不是失败，是额度挡的"

    # 账本：仍然只有 2 行，且状态未变
    ledger = _detail_ledger(client)
    assert len(ledger) == 2
    assert set(ledger.values()) == {"ok"}

    _assert_offline(cli, sock, boss_cls)


# --------------------------------------------------------------------------- #
# B-6（§12 B-6）：幂等复跑，greet 额度充足时也不重发
# --------------------------------------------------------------------------- #
def test_b6_greet_idempotent_when_greet_quota_is_ample(offline):
    """同一库连续两轮，`daily_greet` 足以覆盖"已用量 + 本轮过线数" → 第二轮不重发。

    额度必须**故意给足**（8 = 已用 4 + 过线 4），否则 `greeted=0` 可能来自额度截断，
    那就不是幂等证据。这条与 B-3 是同一个道理：**先排除额度，再谈幂等。**
    """
    client, cli, sock, boss_cls = offline
    # 这条用原装 FakeAdapter：列表项自带 JD，分数稳定在 82/74/74/74（4 条过线）
    adapter = client.app.state.adapter
    assert isinstance(adapter, FakeAdapter)
    client.app.state.config["quota"]["daily_greet"] = 8

    # ---- 第一轮：真发 4 条 ----
    first = _sweep(client)
    assert first["greeted"] == 4
    assert first["passed"] == 4
    assert first["skipped_by_quota"] == 0
    greet_calls_after_first = [c for c in adapter.calls if c.startswith("greet:")]
    assert len(greet_calls_after_first) == 4
    keys_after_first = _greet_keys(client)
    assert keys_after_first == {f"greet:{job_id}" for job_id in CANDIDATE_JOBS}

    used = client.get("/api/status").json()["quota"]["used_today"]
    assert used == 4

    # ---- 第二轮：额度充足（8 ≥ 4 已用 + 4 过线），但不许重发 ----
    quota = client.app.state.config["quota"]
    assert quota["daily_greet"] == 8
    assert quota["daily_greet"] >= used + 4, "额度必须足以覆盖已用量与本轮过线岗位"

    second = _sweep(client)
    assert second["passed"] == 4, "过线岗位数不变（额度不够会让 skipped_by_quota 非 0，这是排除项）"
    assert second["greeted"] == 0, "第二轮不许重发"
    assert second["skipped_by_quota"] == 0, "不是额度截断（额度充足）"

    greet_calls_after_second = [c for c in adapter.calls if c.startswith("greet:")]
    assert greet_calls_after_second == greet_calls_after_first, "greet 调用记录一条都不许增加"
    assert len(greet_calls_after_second) == 4

    keys_after_second = _greet_keys(client)
    assert keys_after_second == keys_after_first, "actions 的 greet 键集合不许变化"
    assert len(keys_after_second) == 4

    _assert_offline(cli, sock, boss_cls)
