"""跨轮 JD 评分回归 —— T1–T5（修复清单 §4）。

缺陷（fix6 `app/api/dashboard.py` 最终评分段）：回读的 `jd_text` 只用于
`compute_gap_flags` 与落库，`matcher.score(job, cfg)` 与 `scored` 里仍是**本轮搜索项**
（正文为空）。于是"第一轮存了 JD、第二轮不重抓"的岗位在第二轮退化为无正文状态：
`passed` 从 4 掉到 0。这一组把"跨轮必须复用已存正文"钉死。

数字全部可手算（`config/matcher.yaml`：`min_match_score=60`，执业领域每词 +8 上限 32）：

| 岗位 | 无正文 | 有正文 |
|---|---:|---:|
| job_001 | 50 | **82** |
| job_002 / 003 / 008 | 50 | **74** |
| job_004 / 005 / 007 | -999（硬淘汰） | -999 |
| job_006 | 0 | 0 |

桩只改适配器返回的数据（`search` 一律给空正文、正文由 `detail` 给），
**不 mock `matcher`**：评分走真实规则，故障注入才有意义。
"""

from __future__ import annotations

from pathlib import Path

import pytest

#: 复用 fix6 的离线护栏（CLI / 真适配器两道硬阻断 + socket 生命周期）
from offline_probe import SocketTripwire, Tripwire, offline_stack

from app import models
from app.adapters.fake import JOBS as FAKE_JOBS
from app.adapters.fake import FakeAdapter
from app.core import matcher
from app.main import create_app

CANDIDATE_JOBS = ("job_001", "job_002", "job_003", "job_008")
_BY_ID = {str(job["job_id"]): dict(job) for job in FAKE_JOBS}

#: T3 用的两份正文：**必须能区分分数与 gap_flags**（不能是两个"都能过线"的文本）
_OLD_JD = str(_BY_ID["job_001"]["jd_text"])  # 4 个领域词 → 82；flags = ["要求2年经验"]
_CURRENT_JD = (
    "岗位职责：负责合同审核、合规检查。"
    "任职要求：持有律师执业证，三年以上企业法务经验。"
)  # 2 个词 → 66；flags = ["要求持证", "要求3年经验"]


# --------------------------------------------------------------------------- #
# 本地桩：列表项没有正文（缺陷现场），正文只能由 detail 或库里给出
# --------------------------------------------------------------------------- #
def _plan(security_ids: dict[str, str] | None = None, jd_text: str = "") -> list[dict]:
    """本轮 `search` 返回的列表项：固定 8 岗、正文为空，可替换 `security_id`。"""
    items: list[dict] = []
    for job in FAKE_JOBS:
        item = dict(job)
        item["jd_text"] = jd_text
        if security_ids and str(job["job_id"]) in security_ids:
            item["security_id"] = security_ids[str(job["job_id"])]
        items.append(item)
    return items


class _ScriptedStub(FakeAdapter):
    """记录真实执行路径的本地桩：`search` 给空正文，`detail` 给正文，`greet` 记账。"""

    def __init__(
        self,
        plan: list[dict] | None = None,
        detail_script: list[dict | None] | None = None,
    ) -> None:
        super().__init__()
        self.set_plan(plan if plan is not None else _plan())
        #: job_id → 上游那一份正文（默认取剧本原文）
        self._jd = {str(job["job_id"]): str(_BY_ID[str(job["job_id"])]["jd_text"]) for job in FAKE_JOBS}
        self._detail_script = list(detail_script or [])
        self.detail_calls: list[str] = []
        self.greet_calls: list[tuple[str, str]] = []

    def set_plan(self, plan: list[dict]) -> None:
        self.plan = [dict(item) for item in plan]

    def probe(self) -> set[str]:
        self.calls.append("probe")
        return set(self.capabilities)

    def search(self, query: str = "", **filters) -> list[dict]:
        self.calls.append(f"search:{query}")
        return [dict(item) for item in self.plan]

    def detail(self, security_id: str, job_id: str | None = None) -> dict:
        key = str(job_id)
        self.detail_calls.append(key)
        if self._detail_script:
            step = self._detail_script.pop(0)
            if step is not None:
                return step  # 失败/风控脚本（形状与 boss.`_fail` 一致）
        return {
            "ok": True,
            "job_id": key,
            "jd_text": self._jd.get(key, ""),
            "address": "",
            "hr_active": "3d",
            "greeted": False,
        }

    def greet(self, security_id: str, job_id: str, message: str) -> dict:
        self.greet_calls.append((str(security_id), str(job_id)))
        return {
            "sent": True,
            "error_code": None,
            "detail": "桩：打招呼成功",
            "raw": {"code": 0},
            "already_greeted": False,
        }


# --------------------------------------------------------------------------- #
# fixture 与工具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def offline(tmp_path: Path, monkeypatch):
    """临时库 + 三只探针；socket 探针按 fix6 的生命周期覆盖请求窗口。"""
    import app.adapters.boss as boss_module
    import app.main as main_module

    cli = Tripwire("真实 Boss CLI 入口 boss._cli_entry")
    sock = SocketTripwire()
    boss_cls = Tripwire("真适配器类 app.main.BossAdapter")
    monkeypatch.setattr(boss_module, "_cli_entry", cli)
    monkeypatch.setattr(main_module, "BossAdapter", boss_cls)

    app = create_app(db_path=tmp_path / "cross_round.db", env_path=tmp_path / "no.env")
    with offline_stack(app, sock=sock) as client:
        yield client, cli, sock, boss_cls


def _use_stub(client, stub: FakeAdapter) -> None:
    client.app.state.adapter = stub
    client.app.state.adapter_name = "cross_round_stub"


def _sweep(client) -> dict:
    result = client.post("/api/run/sweep").json()
    assert result["ok"] is True, result
    return result["stats"]


def _rows(client, job_ids) -> dict[str, dict]:
    """读库里的行。`get_job()` 给的是原始行（`gap_flags` 还是 JSON 文本），
    `list_jobs()` 才会解析 —— 这里用 `parse_gap_flags` 显式对齐，避免拿字符串比 list。"""
    conn = models.connect(client.app.state.db_path)
    try:
        rows = {job_id: (models.get_job(conn, job_id) or {}) for job_id in job_ids}
    finally:
        conn.close()
    for row in rows.values():
        row["gap_flags"] = models.parse_gap_flags(row.get("gap_flags"))
    return rows


def _ledger(client, action_type: str) -> dict[str, str]:
    conn = models.connect(client.app.state.db_path)
    try:
        return {
            row["idempotency_key"]: row["status"]
            for row in conn.execute(
                "SELECT idempotency_key, status FROM actions WHERE action_type = ?",
                (action_type,),
            )
        }
    finally:
        conn.close()


def _assert_offline(cli, sock, boss_cls) -> None:
    assert cli.hits == [], f"调用了真实 Boss CLI：{cli.hits}"
    assert sock.hits == [], f"发生了网络连接：{sock.hits}"
    assert boss_cls.hits == [], f"构造了真适配器：{boss_cls.hits}"


def _spy_prepare(monkeypatch, client):
    """捕获"发送准备"两处入口实际拿到的岗位：`compose_opener` 与 `writer_for`。"""
    import app.api.dashboard as dashboard_module

    compose_jobs: list[dict] = []
    writer_jobs: list[dict] = []
    real_compose = dashboard_module.compose_opener

    def spy_compose(job, resume_summary, llm, *args, **kwargs):
        compose_jobs.append(dict(job))
        return real_compose(job, resume_summary, llm, *args, **kwargs)

    monkeypatch.setattr(dashboard_module, "compose_opener", spy_compose)

    gateway = client.app.state.llm_gateway
    real_writer_for = gateway.writer_for

    def spy_writer_for(job=None):
        writer_jobs.append(dict(job) if job else {})
        return real_writer_for(job)

    monkeypatch.setattr(gateway, "writer_for", spy_writer_for)
    return compose_jobs, writer_jobs


# --------------------------------------------------------------------------- #
# T1 · 同库跨轮：不重抓，但保持最终评分
# --------------------------------------------------------------------------- #
def test_t1_same_db_two_rounds_keep_final_scoring_without_refetch(offline):
    """第一轮抓正文并保存（不发招呼）；第二轮不重抓，**passed 仍必须是 4**。

    `daily_greet=0` 是为了造出"存了正文却没发送"的现场（与 B-3 同一现场），
    同时排除 `already_greeted` 这条判据的干扰。额度充足（50/10）排除额度原因。
    """
    client, cli, sock, boss_cls = offline
    stub = _ScriptedStub()
    _use_stub(client, stub)
    cfg = client.app.state.config
    quota = cfg["quota"]
    assert quota["daily_detail"] == 50, "日额度必须充足，排除额度原因"
    assert quota["detail_batch"] == 10, "单轮额度必须充足（10 > 4 候选）"
    quota["daily_greet"] = 0  # 只存不发

    # ---- 第一轮：4 条候选全部真抓 ----
    first = _sweep(client)
    assert first["enriched"] == 4
    assert sorted(stub.detail_calls) == sorted(CANDIDATE_JOBS)
    assert first["passed"] == 4
    assert first["greeted"] == 0
    assert _ledger(client, "greet") == {}
    detail_ledger_first = _ledger(client, "detail")
    assert len(detail_ledger_first) == 4
    assert set(detail_ledger_first.values()) == {"ok"}

    after_first = _rows(client, CANDIDATE_JOBS)
    for job_id in CANDIDATE_JOBS:
        assert after_first[job_id]["jd_text"] == _BY_ID[job_id]["jd_text"], (
            f"{job_id} 的正文必须已落库"
        )
        expected = matcher.score(dict(_BY_ID[job_id]), cfg)[0]
        assert after_first[job_id]["match_score"] == expected
        assert after_first[job_id]["gap_flags"] == matcher.compute_gap_flags(
            dict(_BY_ID[job_id]), cfg
        )

    # ---- 第二轮：同库同日，search 仍空正文，当日 claim 挡住重抓 ----
    second = _sweep(client)
    assert second["enriched"] == 0, "第二轮不许再抓"
    assert len(stub.detail_calls) == 4, f"detail 总调用必须仍是 4 次：{stub.detail_calls}"
    assert _ledger(client, "detail") == detail_ledger_first, "detail 账本必须与第一轮一致"
    assert second["skipped_by_detail_quota"] == 0, "不是额度挡的（额度充足）"
    assert second["enrich_prefiltered"] == 8, "剪枝 4→8，差额 4 = 当日 claim"
    assert second["greeted"] == 0
    # ↓ 缺陷点：fix6 上这里是 0（评分退回无正文），修好后必须是 4
    assert second["passed"] == 4, "跨轮必须复用已存正文：第二轮 passed 不许从 4 掉到 0"

    after_second = _rows(client, CANDIDATE_JOBS)
    for job_id in CANDIDATE_JOBS:
        assert after_second[job_id]["jd_text"] == after_first[job_id]["jd_text"]
        assert after_second[job_id]["match_score"] == after_first[job_id]["match_score"], (
            f"{job_id} 两轮最终分必须一致"
        )
        assert after_second[job_id]["match_reason"] == after_first[job_id]["match_reason"]
        assert after_second[job_id]["gap_flags"] == after_first[job_id]["gap_flags"]

    _assert_offline(cli, sock, boss_cls)


# --------------------------------------------------------------------------- #
# T2 · 额度恢复后可发送，且传下去的是补齐正文与当前标识
# --------------------------------------------------------------------------- #
def test_t2_quota_recovered_reuses_jd_and_carries_current_identity(offline, monkeypatch):
    """第二轮给足额度 → 真发 4 条；发送准备拿到的正文 = 库里的 JD，`security_id` = 本轮新值。

    第三轮额度同样充足（8 ≥ 已用 4 + 过线 4）→ `greeted=0` 且 `skipped_by_quota=0`：
    **不许用额度不足冒充幂等。**
    """
    client, cli, sock, boss_cls = offline
    stub = _ScriptedStub()
    _use_stub(client, stub)
    quota = client.app.state.config["quota"]
    quota["daily_greet"] = 0  # 第一轮只存不发

    first = _sweep(client)
    assert first["enriched"] == 4
    assert first["passed"] == 4
    assert first["greeted"] == 0

    # ---- 第二轮：只改测试内存额度；search 换成新 security_id，正文仍为空 ----
    quota["daily_greet"] = 8
    new_ids = {job_id: f"sec_{job_id}_r2" for job_id in CANDIDATE_JOBS}
    stub.set_plan(_plan(security_ids=new_ids))

    compose_jobs, writer_jobs = _spy_prepare(monkeypatch, client)

    second = _sweep(client)
    assert second["enriched"] == 0, "第二轮仍不该重抓"
    assert len(stub.detail_calls) == 4
    assert second["passed"] == 4, "跨轮复用正文后必须仍然 4 条过线"
    assert second["greeted"] == 4

    greet_ledger = _ledger(client, "greet")
    assert len(greet_ledger) == 4
    assert set(greet_ledger.values()) == {"ok"}

    # 发送准备入口拿到的必须是补齐后的正文
    assert len(compose_jobs) == 4, compose_jobs
    assert len(writer_jobs) == 4, writer_jobs
    for job in compose_jobs:
        assert job["jd_text"] == _BY_ID[str(job["job_id"])]["jd_text"], (
            "compose_opener 必须拿到补齐后的正文"
        )
    for job in writer_jobs:
        assert job["jd_text"] == _BY_ID[str(job["job_id"])]["jd_text"], (
            "writer_for 必须拿到补齐后的正文"
        )

    # 传下去的是**本轮**的标识，不是旧行里的
    assert {security_id for security_id, _job_id in stub.greet_calls} == set(new_ids.values()), (
        f"greet 必须用第二轮的新 security_id：{stub.greet_calls}"
    )
    assert set(job["security_id"] for job in compose_jobs) == set(new_ids.values())

    # ---- 第三轮：额度充足，但不许重发 ----
    compose_jobs.clear()
    writer_jobs.clear()
    third = _sweep(client)
    assert third["passed"] == 4
    assert third["greeted"] == 0, "第三轮不许重发"
    assert third["skipped_by_quota"] == 0, "不是额度截断（额度充足）"
    assert len(stub.greet_calls) == 4, "greet 调用数一条都不许增加"
    assert _ledger(client, "greet") == greet_ledger
    assert compose_jobs == [] and writer_jobs == [], "claim 失败就该连生成都不做"

    _assert_offline(cli, sock, boss_cls)


# --------------------------------------------------------------------------- #
# T3 · 当前正文优先于旧库正文
# --------------------------------------------------------------------------- #
def test_t3_current_jd_wins_over_stored_jd(offline, monkeypatch):
    """两份正文给出**不同分数与不同 gap_flags**，证明"当前优先"真的生效。

    旧正文 82 分 / flags=["要求2年经验"]；当前正文 66 分 / flags=["要求持证","要求3年经验"]。
    只回退 `jd_text`：`title` / `salary` / `security_id` 等仍取本轮的值。
    """
    client, cli, sock, boss_cls = offline
    cfg = client.app.state.config

    # 预置：库里已有同一 job_id 的旧正文（上一轮抓的）
    conn = models.connect(client.app.state.db_path)
    try:
        models.upsert_jobs(conn, [dict(_BY_ID["job_001"], jd_text="")])
        models.update_job_detail(
            conn,
            "job_001",
            _OLD_JD,
            matcher.compute_gap_flags(dict(_BY_ID["job_001"], jd_text=_OLD_JD), cfg),
        )
    finally:
        conn.close()

    # 本轮：同一 job_id，但正文不同，其他字段也换了（检验不整行替换）
    current = dict(
        _BY_ID["job_001"],
        jd_text=_CURRENT_JD,
        security_id="sec_001_current",
        title="法务专员（合同）",
        salary="6-9K",
    )
    stub = _ScriptedStub(plan=[current])
    _use_stub(client, stub)
    client.app.state.config["quota"]["daily_greet"] = 5

    compose_jobs, _writer_jobs = _spy_prepare(monkeypatch, client)

    stats = _sweep(client)
    assert stats["enriched"] == 0, "列表项自带非空正文 → 不该再抓 detail"
    assert stub.detail_calls == []
    assert stats["scored"] == 1

    current_score = matcher.score(dict(current), cfg)[0]
    old_score = matcher.score(dict(_BY_ID["job_001"], jd_text=_OLD_JD), cfg)[0]
    current_flags = matcher.compute_gap_flags(dict(current), cfg)
    old_flags = matcher.compute_gap_flags(dict(_BY_ID["job_001"], jd_text=_OLD_JD), cfg)
    assert current_score != old_score, "两份正文必须能区分分数，否则这条测试证明不了优先级"
    assert current_flags != old_flags, "两份正文必须能区分 gap_flags"

    row = _rows(client, ["job_001"])["job_001"]
    assert row["jd_text"] == _CURRENT_JD, "落库必须是当前正文，不许被旧正文盖掉"
    assert row["match_score"] == current_score, "最终评分必须基于当前正文"
    assert row["gap_flags"] == current_flags, "gap_flags 必须基于当前正文"

    # 只回退正文：本轮岗位的其他字段不许被旧行替换
    assert len(compose_jobs) == 1, compose_jobs
    handed = compose_jobs[0]
    assert handed["jd_text"] == _CURRENT_JD
    assert handed["title"] == "法务专员（合同）"
    assert handed["salary"] == "6-9K"
    assert stub.greet_calls == [("sec_001_current", "job_001")], stub.greet_calls

    _assert_offline(cli, sock, boss_cls)


# --------------------------------------------------------------------------- #
# T4 · 无正文时不伪造，也不串岗
# --------------------------------------------------------------------------- #
def test_t4_missing_jd_is_neither_faked_nor_borrowed(offline):
    """同一份库里别的岗位有高分正文，但**不许借用**；本岗保持空正文、分数=真实空正文结果。"""
    client, cli, sock, boss_cls = offline
    cfg = client.app.state.config

    # 库里另有一个岗位（本轮不返回）已存高分正文
    conn = models.connect(client.app.state.db_path)
    try:
        models.upsert_jobs(conn, [dict(_BY_ID["job_002"], jd_text="")])
        models.update_job_detail(
            conn,
            "job_002",
            _BY_ID["job_002"]["jd_text"],
            matcher.compute_gap_flags(dict(_BY_ID["job_002"]), cfg),
        )
    finally:
        conn.close()

    blank = dict(_BY_ID["job_001"], job_id="job_009", security_id="sec_009", jd_text="")
    stub = _ScriptedStub(plan=[blank])
    _use_stub(client, stub)
    client.app.state.config["quota"]["daily_detail"] = 0  # 排除抓取，正文不可能被补上

    stats = _sweep(client)
    assert stats["enriched"] == 0
    assert stats["skipped_by_detail_quota"] >= 1, "必须是被日额度挡住的，不是没候选"
    assert stats["scored"] == 1, "只有本轮返回的岗位进入评分"

    empty_score = matcher.score(dict(blank), cfg)[0]
    borrowed_score = matcher.score(dict(_BY_ID["job_002"]), cfg)[0]
    assert empty_score != borrowed_score, "两个分数必须能区分，否则证明不了不串岗"

    row = _rows(client, ["job_009"])["job_009"]
    assert (row["jd_text"] or "") == "", "没有正文就保持空，不许伪造"
    assert row["match_score"] == empty_score, "评分必须是真实 matcher 对空正文的结果"
    assert row["match_score"] != borrowed_score, "不许借用另一个岗位的正文"
    assert row["gap_flags"] == [], "空正文不应凭空产生 gap_flags"
    assert stats["passed"] == 0
    assert stub.greet_calls == []

    _assert_offline(cli, sock, boss_cls)


# --------------------------------------------------------------------------- #
# T5 · 风控门控保持
# --------------------------------------------------------------------------- #
def test_t5_risk_gate_still_blocks_final_scoring_and_sending(offline, monkeypatch):
    """detail 第一条成功、第二条风控 → 正文与 gap_flags 仍落库，但**不最终打分、不发送**。

    区分"允许的预打分"与"禁止的最终打分"：预打分时列表项正文为空，最终打分时正文非空。
    所以断言"没有任何一次 score 调用带非空正文"，而不是"matcher 零调用"。
    """
    client, cli, sock, boss_cls = offline
    risk_fail = {
        "ok": False,
        "error": {"code": "ENVIRONMENT_RISK", "message": "命中环境风控"},
    }
    stub = _ScriptedStub(detail_script=[None, risk_fail])  # 第 1 条成功，第 2 条风控
    _use_stub(client, stub)
    client.app.state.config["quota"]["daily_greet"] = 5

    score_calls: list[tuple[str, str]] = []
    real_score = matcher.score

    def spy_score(job, merged_cfg):
        score_calls.append((str(job.get("job_id")), str(job.get("jd_text") or "")))
        return real_score(job, merged_cfg)

    monkeypatch.setattr(matcher, "score", spy_score)
    compose_jobs, writer_jobs = _spy_prepare(monkeypatch, client)

    stats = _sweep(client)

    # 抓到了 1 条就停：风控锁 + 整轮终止
    assert stats["enriched"] == 1, stats
    assert len(stub.detail_calls) == 2, stub.detail_calls
    assert stats["risk_locked"] is True, "风控必须锁"
    assert client.get("/api/status").json()["risk_locked"] is True

    # 已经拿到的正文与 gap_flags 必须落库（F7 口径不动）
    row = _rows(client, ["job_001"])["job_001"]
    assert row["jd_text"] == _BY_ID["job_001"]["jd_text"], "抓到的正文必须落库"
    assert row["gap_flags"] == matcher.compute_gap_flags(
        dict(_BY_ID["job_001"]), client.app.state.config
    )

    # 预打分允许（正文为空），最终打分禁止（正文非空）
    assert score_calls, "预打分必须真的发生过，否则这条断言没有区分力"
    assert all(not jd_text for _job_id, jd_text in score_calls), (
        f"风控后不许再做最终评分：{score_calls}"
    )
    assert row["match_score"] is None, "风控后不许写最终分"

    # 生成与发送一律未执行
    assert compose_jobs == [] and writer_jobs == []
    assert stub.greet_calls == []
    assert stats["greeted"] == 0

    _assert_offline(cli, sock, boss_cls)
