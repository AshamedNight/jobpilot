"""fix9：直发停轮与错误记录（F1–F4）的定向回归。

本文件是 fix9 修单 §3 的八条行为，独立实现（不照搬 `test_direct_greet.py` 的组织方式，
只复用它的离线护栏口径：临时库 + CLI / 真适配器类 / socket 三层硬阻断 + “评分/模板/LLM
一碰就炸”）。

对号：

1. ``test_f1_*``  —— 搜索失败（普通 / 风险 / 空岗位）先停，零 greet 零 claim。
2. ``test_f2_*``  —— 停轮判据同时覆盖 raw 与 normalized，且已成功的新发不被改判。
3. ``test_f3_*``  —— 每次调用后保存“最后一次真实失败摘要”，与 runs 同源。
4. ``test_f4_*``  —— 反查 / 会话同步异常：成功账本不变、告警有值、停轮、不泄漏原文。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from offline_probe import SocketTripwire, Tripwire, offline_stack

import app.core.matcher as matcher_module
import app.api.dashboard as dashboard_module
from app import models
from app.adapters.base import err_result, ok_result
from app.adapters.fake import JOBS as FAKE_JOBS
from app.adapters.fake import FakeAdapter
from app.core import idempotency as idem
from app.main import create_app

EXPECTED_MESSAGE = "您好，我对这个岗位感兴趣，希望进一步了解岗位情况。"

_BY_ID = {str(job["job_id"]): dict(job) for job in FAKE_JOBS}


def _job(job_id: str, **overrides) -> dict:
    item = dict(_BY_ID[job_id])
    item.update(overrides)
    return item


class _Fix9Stub(FakeAdapter):
    """可编排 search / greet / chat 的直发桩。

    **关键点**：`last_error` 只在剧本显式要求时写入，用来模拟真适配器“上游失败落在
    适配器属性上”的行为（`FakeAdapter` 本身没有这个属性）。默认不写 → 逼着生产代码
    在只有 `result.error_code` 时也要能生成最小安全摘要（修单 F3 最后一段）。
    """

    def __init__(self, plan=None, search_error=None, greet_script=None, chat_script=None):
        super().__init__()
        self.plan = [dict(item) for item in (plan if plan is not None else [])]
        self._search_error = dict(search_error) if search_error else None
        self._greet_script = list(greet_script or [])
        self._chat_script = list(chat_script or [])
        self.detail_calls: list[str] = []
        self.greet_calls: list[tuple[str, str, str]] = []
        self.chat_calls: list[str] = []
        self.search_calls: list[str] = []
        self.last_error = None

    # -- 读 ---------------------------------------------------------------- #
    def search(self, query: str = "", **filters) -> list[dict]:
        self.calls.append(f"search:{query}")
        self.search_calls.append(f"search:{query}")
        if self._search_error is not None:
            # 真适配器分页：**先拿到一部分岗位，再报错**（修单 F1 明确不许靠 pages=1 兜着）
            self.last_error = dict(self._search_error)
        return [dict(item) for item in self.plan]

    def chat(self, **filters) -> list[dict]:
        self.chat_calls.append("chat")
        step = self._chat_script.pop(0) if self._chat_script else "default"
        if step == "raise":
            raise RuntimeError("会话反查炸了 token=REVIEW_SENTINEL")
        if isinstance(step, dict):
            self.last_error = dict(step)
            return []
        if step == "empty":
            self.last_error = None
            return []
        return super().chat(**filters)

    def detail(self, security_id: str, job_id: str | None = None) -> dict:
        self.detail_calls.append(str(job_id))
        raise AssertionError("搜索直发批次不许调用 detail")

    # -- 写 ---------------------------------------------------------------- #
    def greet(self, security_id: str, job_id: str, message: str) -> dict:
        self.greet_calls.append((str(security_id), str(job_id), message))
        step = self._greet_script.pop(0) if self._greet_script else "ok"
        if step == "ok":
            self._greeted[job_id] = f"fake_{job_id}"
            self._greeting_text[job_id] = message
            return ok_result("剧本：发送成功", raw={"fake": True})
        if step == "already":
            return err_result("ALREADY_GREETED", "剧本：平台早已打过招呼")
        if step == "risk":
            return err_result("ACCOUNT_RISK", "剧本：命中风控")
        if step == "limit":
            return err_result("GREET_LIMIT", "剧本：今日额度已用尽")
        if step == "auth":
            return err_result("AUTH_EXPIRED", "剧本：登录态失效")
        if step == "fail":
            # 只有规范码、**不写 last_error** → 生产代码必须自己合成最小安全摘要
            return err_result("UPSTREAM_ERROR", "剧本：普通失败，且不带 last_error")
        if isinstance(step, dict):
            # 专项：raw 不在既有族名里，只有 normalized 能分类（F2 的“不许被 raw 遮掉”）
            self.last_error = dict(step)
            return err_result(str(step.get("code")), "剧本：raw 不在既有族里")
        raise AssertionError(f"未知 greet 剧本步骤 {step!r}")


@pytest.fixture()
def offline(tmp_path: Path, monkeypatch):
    import app.adapters.boss as boss_module
    import app.main as main_module

    cli = Tripwire("真实 Boss CLI 入口 boss._cli_entry")
    sock = SocketTripwire()
    boss_cls = Tripwire("真适配器类 app.main.BossAdapter")
    monkeypatch.setattr(boss_module, "_cli_entry", cli)
    monkeypatch.setattr(main_module, "BossAdapter", boss_cls)

    app = create_app(db_path=tmp_path / "fix9.db", env_path=tmp_path / "no.env")

    def boom(*args, **kwargs):
        raise AssertionError("搜索直发批次不许调用评分 / 模板 / LLM")

    monkeypatch.setattr(matcher_module, "score", boom, raising=True)
    monkeypatch.setattr(dashboard_module, "compose_opener", boom, raising=True)
    monkeypatch.setattr(app.state.llm_gateway, "writer_for", boom, raising=True)

    with offline_stack(app, sock=sock) as client:
        yield client, cli, sock, boss_cls


def _post(client) -> dict:
    response = client.post("/api/run/direct-greet")
    assert response.status_code == 200, response.text
    return response.json()


def _use(client, stub: FakeAdapter) -> FakeAdapter:
    client.app.state.adapter = stub
    client.app.state.adapter_name = "fix9_stub"
    return stub


def _ledger(client) -> dict[str, str]:
    conn = models.connect(client.app.state.db_path)
    try:
        return {
            row["idempotency_key"]: row["status"]
            for row in conn.execute(
                "SELECT idempotency_key, status FROM actions WHERE action_type = 'greet'"
            )
        }
    finally:
        conn.close()


def _latest_run(client) -> dict:
    conn = models.connect(client.app.state.db_path)
    try:
        rows = models.list_runs(conn, limit=1)
        run = dict(rows[0]) if rows else {}
    finally:
        conn.close()
    if run.get("stats_json"):
        run["stats"] = json.loads(run["stats_json"])
    else:
        run["stats"] = {}
    return run


def _conversation_exists(client, uid: str) -> bool:
    conn = models.connect(client.app.state.db_path)
    try:
        return models.get_conversation(conn, uid) is not None
    finally:
        conn.close()


def _assert_offline(cli, sock, boss_cls) -> None:
    assert cli.hits == [], f"调用了真实 Boss CLI：{cli.hits}"
    assert sock.hits == [], f"发生了网络连接：{sock.hits}"
    assert boss_cls.hits == [], f"构造了真适配器：{boss_cls.hits}"


def _assert_summary_consistent(client, result: dict) -> dict:
    """F3 的核心判据：响应 stats / 顶层 / runs.stats_json 三处同源。"""
    summary = result["stats"].get("upstream_error")
    assert summary, f"应当留下本轮上游失败摘要：{result['stats']}"
    assert result.get("upstream_error") == summary, "顶层 upstream_error 必须与 stats 同源"
    run = _latest_run(client)
    assert run["stats"].get("upstream_error") == summary, "runs.stats_json 必须与响应同源"
    return summary


# =========================================================================== #
# 1 · F1：搜索失败先停（普通 / 风险 / 空岗位）
# =========================================================================== #
def test_f1_partial_jobs_with_ordinary_search_error_sends_nothing(offline):
    """部分岗位 + **普通**搜索错误：零 greet / 零 claim，摘要三处一致，且不永久锁风险。"""
    client, cli, sock, boss_cls = offline
    stub = _use(
        client,
        _Fix9Stub(
            plan=[_job("job_001"), _job("job_002")],
            search_error={"command": "search", "code": "INVALID_PARAM", "message": "剧本：参数非法"},
        ),
    )
    client.app.state.config["quota"]["daily_greet"] = 5

    result = _post(client)
    stats = result["stats"]

    assert result["ok"] is False, result
    assert stub.greet_calls == [], f"搜索失败后一条都不许发：{stub.greet_calls}"
    assert _ledger(client) == {}, "搜索失败后不许留下任何 greet claim"
    assert stats["sent_new"] == 0
    summary = _assert_summary_consistent(client, result)
    assert summary["code"] == "INVALID_PARAM", summary

    # 普通错误只停本轮：不许把它当风控永久锁
    assert stats["risk_locked"] is False, stats
    assert client.get("/api/status").json()["risk_locked"] is False

    # 落库允许保留（部分岗位可追溯），但绝不触发发送/评分/详情
    assert stub.detail_calls == []

    _assert_offline(cli, sock, boss_cls)


def test_f1_partial_jobs_with_search_risk_locks_and_blocks_next_call(offline):
    """部分岗位 + 搜索**风控**：零 greet/claim、risk_locked=true、下次请求在 search 前拒绝。"""
    client, cli, sock, boss_cls = offline
    stub = _use(
        client,
        _Fix9Stub(
            plan=[_job("job_001"), _job("job_002")],
            search_error={"command": "search", "code": "ENVIRONMENT_RISK", "message": "剧本：搜索命中风控"},
        ),
    )
    client.app.state.config["quota"]["daily_greet"] = 5

    result = _post(client)
    stats = result["stats"]
    assert result["ok"] is False, result
    assert stub.greet_calls == [], "搜索风控后一条都不许发"
    assert _ledger(client) == {}, "搜索风控后不许留下任何 greet claim"
    assert stats["risk_locked"] is True, stats
    assert client.get("/api/status").json()["risk_locked"] is True, "必须置共用风险锁"
    _assert_summary_consistent(client, result)

    # 下一次调用：必须在 search 之前就拒绝（不许再碰上游）
    searches_before = len(stub.search_calls)
    again = _post(client)
    assert again["ok"] is False, again
    assert "风控" in again["error"], again
    assert len(stub.search_calls) == searches_before, "锁着的时候不许先搜索"

    _assert_offline(cli, sock, boss_cls)


def test_f1_empty_jobs_with_search_risk_locks_project(offline):
    """**空**岗位 + 搜索风控：同样锁项目（不能只在“有岗位”时才锁）。"""
    client, cli, sock, boss_cls = offline
    stub = _use(
        client,
        _Fix9Stub(
            plan=[],
            search_error={"command": "search", "code": "ACCOUNT_RISK", "message": "剧本：账号风控"},
        ),
    )
    client.app.state.config["quota"]["daily_greet"] = 5

    result = _post(client)
    stats = result["stats"]
    assert result["ok"] is False, result
    assert stats["found"] == 0
    assert stats["risk_locked"] is True, stats
    assert client.get("/api/status").json()["risk_locked"] is True
    assert stub.greet_calls == []
    assert _ledger(client) == {}
    _assert_summary_consistent(client, result)

    searches_before = len(stub.search_calls)
    _post(client)
    assert len(stub.search_calls) == searches_before, "空岗位风控后也要在 search 前拒绝"

    _assert_offline(cli, sock, boss_cls)


# =========================================================================== #
# 2 · F2：停轮判据同时覆盖 raw 与 normalized
# =========================================================================== #
@pytest.mark.parametrize(
    "code,normalized",
    [
        ("REQUEST_BUDGET_EXHAUSTED", "LIMIT"),
        ("RATE_LIMITED", "LIMIT"),
        ("GREET_LIMIT", "LIMIT"),
    ],
)
def test_f2_chat_stop_codes_keep_sent_and_stop_after_first(offline, code, normalized):
    """chat 的预算/限流族：只发第一条，claim=ok、sent_new=1，告警与摘要有值，第二条无 claim。"""
    client, cli, sock, boss_cls = offline
    stub = _use(
        client,
        _Fix9Stub(
            plan=[_job("job_001"), _job("job_002")],
            chat_script=[{"command": "chat", "code": code, "normalized": normalized, "message": "剧本"}],
        ),
    )
    client.app.state.config["quota"]["daily_greet"] = 5

    result = _post(client)
    stats = result["stats"]

    assert stats["sent_new"] == 1, f"{code} 不许把已成功的新发改判：{stats}"
    assert len(stub.greet_calls) == 1, f"{code} 之后必须停轮，不许发下一个岗位"
    assert stub.greet_calls[0][1] == "job_001"

    ledger = _ledger(client)
    assert ledger[idem.greet_key("job_001")] == "ok", "已成功的新发 claim 必须是 ok"
    assert idem.greet_key("job_002") not in ledger, "停轮后下一个岗位不许留下 claim"

    assert stats["conversation_warnings"], "停轮必须留下可读告警"
    _assert_summary_consistent(client, result)

    _assert_offline(cli, sock, boss_cls)


@pytest.mark.parametrize(
    "normalized",
    ["LIMIT", "AUTH", "RISK_LOCKED"],
)
def test_f2_normalized_only_codes_are_not_shadowed_by_raw(offline, normalized):
    """raw 不在既有族名里、只有 normalized 能分类：必须照样停轮（risk 还要锁 + 下次拒绝）。"""
    client, cli, sock, boss_cls = offline
    stub = _use(
        client,
        _Fix9Stub(
            plan=[_job("job_001"), _job("job_002")],
            greet_script=[
                {"code": "UNMAPPED_RAW_XYZ", "normalized": normalized, "message": "剧本：只有归一码"},
            ],
        ),
    )
    client.app.state.config["quota"]["daily_greet"] = 5

    result = _post(client)
    stats = result["stats"]

    assert len(stub.greet_calls) == 1, f"normalized={normalized} 必须停轮，不许发第二个岗位"
    assert stats["failed"] == 1, stats
    summary = _assert_summary_consistent(client, result)
    assert summary["normalized"] == normalized, summary

    if normalized == "RISK_LOCKED":
        assert stats["risk_locked"] is True, stats
        searches_before = len(stub.search_calls)
        again = _post(client)
        assert again["ok"] is False, again
        assert len(stub.search_calls) == searches_before, "normalized=RISK_LOCKED 也要在 search 前拒绝"
    else:
        assert stats["risk_locked"] is False, stats

    _assert_offline(cli, sock, boss_cls)


# =========================================================================== #
# 3 · F3：greet 失败的摘要与 runs 同源；普通失败后成功不擦掉上一错误
# =========================================================================== #
@pytest.mark.parametrize("step", ["auth", "risk", "limit"])
def test_f3_greet_family_failures_are_summarized_and_stop(offline, step):
    """认证 / 风控 / 限流：停轮 + 摘要可见 + 三处同源（只有规范码时也要合成摘要）。"""
    client, cli, sock, boss_cls = offline
    stub = _use(
        client,
        _Fix9Stub(plan=[_job("job_001"), _job("job_002")], greet_script=[step]),
    )
    client.app.state.config["quota"]["daily_greet"] = 5

    result = _post(client)
    stats = result["stats"]

    assert len(stub.greet_calls) == 1, f"{step} 必须停轮"
    assert stats["failed"] == 1, stats
    summary = _assert_summary_consistent(client, result)
    assert summary["code"], summary
    assert summary["normalized"], "摘要必须带归一码，否则下游无法分类"
    # 摘要里不许出现敏感标识
    assert "security_id" not in json.dumps(summary, ensure_ascii=False)

    _assert_offline(cli, sock, boss_cls)


def test_f3_ordinary_greet_failure_summary_survives_later_success(offline):
    """普通失败：继续下一条；**后来成功也不许擦掉**上一轮真实失败摘要。"""
    client, cli, sock, boss_cls = offline
    stub = _use(
        client,
        _Fix9Stub(plan=[_job("job_001"), _job("job_002")], greet_script=["fail", "ok"]),
    )
    client.app.state.config["quota"]["daily_greet"] = 5

    result = _post(client)
    stats = result["stats"]

    assert len(stub.greet_calls) == 2, "普通失败不停轮，下一条要照发"
    assert stats["failed"] == 1 and stats["sent_new"] == 1, stats
    summary = _assert_summary_consistent(client, result)
    assert summary["code"] == "UPSTREAM_ERROR", summary
    assert summary["command"] == "greet", summary

    _assert_offline(cli, sock, boss_cls)


def test_f3_already_greeted_leaves_no_fake_failure(offline):
    """normalized=OK 的幂等成功：不算失败、不写摘要、不成为停轮理由。"""
    client, cli, sock, boss_cls = offline
    stub = _use(
        client,
        _Fix9Stub(plan=[_job("job_001"), _job("job_002")], greet_script=["already", "ok"]),
    )
    client.app.state.config["quota"]["daily_greet"] = 5

    result = _post(client)
    stats = result["stats"]

    assert stats["already_greeted"] == 1, stats
    assert stats["failed"] == 0, "already 不是失败"
    assert stats["upstream_error"] is None, f"already 不许留下假失败摘要：{stats}"
    assert len(stub.greet_calls) == 2, "already 不停轮"
    assert _latest_run(client)["stats"].get("upstream_error") is None
    assert result["ok"] is True, result

    _assert_offline(cli, sock, boss_cls)


# =========================================================================== #
# 4 · F4：反查 / 会话同步异常
# =========================================================================== #
def test_f4_chat_exception_keeps_ledger_and_hides_sentinel(offline):
    """反查抛异常：sent_new=1、claim=ok、告警非空、停轮、不重发，且原文不出现在接口与 runs。"""
    client, cli, sock, boss_cls = offline
    stub = _use(
        client,
        _Fix9Stub(
            plan=[_job("job_001"), _job("job_002")],
            chat_script=["raise"],
        ),
    )
    client.app.state.config["quota"]["daily_greet"] = 5

    result = _post(client)
    stats = result["stats"]

    assert stats["sent_new"] == 1, f"反查异常不许改判已发送：{stats}"
    assert len(stub.greet_calls) == 1, "异常后必须停轮，不许继续下一个岗位"
    assert _ledger(client)[idem.greet_key("job_001")] == "ok", "claim 必须保持 ok"
    assert stats["conversation_warnings"], "异常必须留下可读告警"
    warning = " ".join(stats["conversation_warnings"])
    assert "已发送" in warning and "不重试" in warning, warning
    assert result["ok"] is False, "运行异常但发送已发生 → ok=false，需在交付说明里明确判读"
    assert result.get("run_id"), result

    # 哨兵不许漏到接口 / runs
    assert "REVIEW_SENTINEL" not in json.dumps(result, ensure_ascii=False), "接口泄漏了异常原文"
    run = _latest_run(client)
    assert "REVIEW_SENTINEL" not in (run.get("error") or ""), "runs.error 泄漏了异常原文"
    assert "REVIEW_SENTINEL" not in json.dumps(run.get("stats") or {}, ensure_ascii=False)

    # 再跑一轮：只让桩返回那条已 claim 的岗位 —— 一条都不许重发
    greets_before = len(stub.greet_calls)
    stub.plan = [_job("job_001")]
    _post(client)
    assert len(stub.greet_calls) == greets_before, "异常之后不许自动重发"

    _assert_offline(cli, sock, boss_cls)


def test_f4_conversation_sync_exception_keeps_ledger_and_warns(offline, monkeypatch):
    """会话落库抛异常：成功账本不变、告警非空、不重发、不伪造完整同步证据。"""
    client, cli, sock, boss_cls = offline
    stub = _use(
        client,
        _Fix9Stub(plan=[_job("job_001"), _job("job_002")]),
    )
    client.app.state.config["quota"]["daily_greet"] = 5

    real_upsert = models.upsert_conversation

    def boom_upsert(*args, **kwargs):
        raise RuntimeError("会话落库炸了 token=REVIEW_SENTINEL")

    monkeypatch.setattr(models, "upsert_conversation", boom_upsert, raising=True)

    result = _post(client)
    stats = result["stats"]

    assert stats["sent_new"] == 1, f"落库异常不许改判已发送：{stats}"
    assert len(stub.greet_calls) == 1, "落库异常后必须停轮"
    assert _ledger(client)[idem.greet_key("job_001")] == "ok"
    assert stats["conversation_warnings"], "落库异常必须留下可读告警"
    warning = " ".join(stats["conversation_warnings"])
    assert "未同步" in warning and "不重试" in warning, warning
    assert result["ok"] is False, result
    assert "REVIEW_SENTINEL" not in json.dumps(result, ensure_ascii=False)

    # 不伪造“同步完成”的证据：会话行不该存在
    assert not _conversation_exists(client, "fake_job_001"), "uid 已知但落库失败，不许留下半截会话"

    greets_before = len(stub.greet_calls)
    monkeypatch.setattr(models, "upsert_conversation", real_upsert, raising=True)
    stub.plan = [_job("job_001")]
    _post(client)
    assert len(stub.greet_calls) == greets_before, "异常之后不许自动重发"

    _assert_offline(cli, sock, boss_cls)
