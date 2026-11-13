"""fix10：错误识别与异常风险保留（R1 / R2）的定向回归。

依据 `修单-Phase2-B_fix10-错误识别与异常风险保留.md`。基线 fix9（16 条新测试全绿仍留在
`test_direct_greet_fix9.py` 里，本文件**不重跑、不改动**它们）。

只覆盖 fix9 复审登记的两条：

- **R1**：`_new_failure` 里 `after == before` 会吞掉"新建但内容相等"的错误 dict。
  真实 adapter 没有错误时间戳，连续同样失败完全可能 → 第二次失败被当成"没发生"，
  于是带着一次失败的搜索照常进入 greet。两面都要钉住：
  ① 每次真实调用新产生的失败都必须捕获（**哪怕与之前内容完全相同**）；
  ② 只是留着上一阶段同一个旧属性的桩，**不许**误报。
- **R2**：反查异常分支跳过了本次可靠摘要与风险分类。chat 返回后的摘要与分类
  **不能等到会话同步成功**才做；RISK_LOCKED / raw 风险族仍置共用锁，AUTH / LIMIT
  仍停轮且摘要可见，没有可靠分类不伪造。

离线口径与 fix9 完全一致（复用 `test_direct_greet_fix9` 的 `offline` fixture：临时库 +
CLI / 真适配器类 / socket 三层硬阻断 + 评分/模板/LLM 一碰就炸）。**零真实平台 IO。**
"""

from __future__ import annotations

import json

import pytest

from app import models
from app.adapters.boss import BossAdapter
from app.core import idempotency as idem

from test_direct_greet_fix9 import (  # noqa: F401  （`offline` 是被 pytest 当 fixture 用的）
    _assert_offline,
    _conversation_exists,
    _job,
    _latest_run,
    _ledger,
    _post,
    _use,
    _Fix9Stub,
    offline,
)


def _record(adapter, command: str, code: str, message: str) -> None:
    """用**真实适配器的写入函数**造假：它每次都会 `self.last_error = {**...}` 新建一个 dict。

    这正是 R1 的现场——content 相等、**对象是新的**。用 `len()` 之类的比较就会漏掉它。
    """
    BossAdapter._record_upstream_failure(
        adapter, command, {"error": {"code": code, "message": message}}
    )


# =========================================================================== #
# R1 · 新错误不能按内容相等去重
# =========================================================================== #
def test_r1_repeated_equal_search_error_never_sends(offline):
    """连续两轮**内容完全相同**的 search 普通错误 + 部分岗位：第二轮仍零 greet / 零 claim。"""
    client, cli, sock, boss_cls = offline
    stub = _use(client, _Fix9Stub(plan=[_job("job_001"), _job("job_002")]))

    def search(*args, **kwargs):
        # 真实写入函数：每次都是新建 dict，内容一模一样
        _record(stub, "search", "INVALID_PARAM", "review synthetic same error")
        return [dict(item) for item in stub.plan]  # 分页已拿到一部分岗位

    stub.search = search
    client.app.state.config["quota"]["daily_greet"] = 1

    first = _post(client)
    assert first["ok"] is False, first
    assert first["stats"]["sent_new"] == 0, first["stats"]
    assert stub.greet_calls == [], "第一轮就不许发"
    assert _ledger(client) == {}

    second = _post(client)
    assert second["ok"] is False, f"第二轮把相同失败当成没发生：{second}"
    assert stub.greet_calls == [], f"repeated equal search failure entered greet: {second}"
    assert _ledger(client) == {}, "第二轮也不许留下任何 greet claim"
    assert second["stats"]["upstream_error"], second["stats"]
    assert second["stats"]["upstream_error"]["code"] == "INVALID_PARAM"
    run = _latest_run(client)
    assert run["stats"]["upstream_error"] == second["stats"]["upstream_error"], "摘要必须与 runs 同源"

    # 普通错误只停本轮，**不许**永久置风险锁
    assert second["stats"]["risk_locked"] is False, second["stats"]
    assert client.get("/api/status").json()["risk_locked"] is False

    _assert_offline(cli, sock, boss_cls)


def test_r1_real_search_repeated_equal_partial_failure_stops(offline):
    """真 `BossAdapter.search` 两页路径：第 1 页有岗位、第 2 页相同失败 —— 两轮都不许发。

    只替换 CLI IO（`_run_cli`）与两个纯函数绑定，**不构造真适配器、不出网**。
    """
    client, cli, sock, boss_cls = offline
    stub = _use(client, _Fix9Stub())
    stub.pages = 2
    stub.active = "online"
    stub.search_url = "https://example.invalid/review"

    def run_cli(*args):
        stub.last_error = None  # 与真实 `_run_cli` 一致：每次调用先把旧错误清掉
        if args[-1] == "1":
            return {
                "ok": True,
                "data": [{"job_id": "job_001", "security_id": "sec_001", "title": "review"}],
                "pagination": {"has_more": True},
            }
        return {"ok": False, "error": {"code": "INVALID_PARAM", "message": "same synthetic failure"}}

    stub._run_cli = run_cli
    stub._as_list = BossAdapter._as_list
    stub._map_job = lambda item: BossAdapter._map_job(stub, item)
    stub._record_upstream_failure = lambda cmd, env: BossAdapter._record_upstream_failure(stub, cmd, env)
    stub.search = lambda *a, **kw: BossAdapter.search(stub, *a, **kw)
    client.app.state.config["quota"]["daily_greet"] = 1

    first = _post(client)
    assert first["ok"] is False, first
    assert first["stats"]["found"] == 1, "第 1 页的岗位要保留（部分岗位可落库）"
    assert stub.greet_calls == []

    second = _post(client)
    _assert_offline(cli, sock, boss_cls)
    assert stub.greet_calls == [], f"分页部分成功 + 相同失败，第二轮进入 greet：{second}"
    assert second["ok"] is False, second
    assert _ledger(client) == {}
    assert second["stats"]["upstream_error"]["code"] == "INVALID_PARAM", second["stats"]


def test_r1_repeated_equal_chat_failure_keeps_this_round_summary(offline):
    """连续**相同内容**的 chat 失败、不同岗位：第二轮仍要留下本轮摘要（不许被内容去重吞掉）。"""
    client, cli, sock, boss_cls = offline
    stub = _use(client, _Fix9Stub(plan=[_job("job_001")]))

    def chat(**kwargs):
        _record(stub, "chat", "UPSTREAM_TIMEOUT", "review synthetic same chat error")
        return []

    stub.chat = chat
    client.app.state.config["quota"]["daily_greet"] = 5

    first = _post(client)
    assert first["stats"]["sent_new"] == 1, first["stats"]
    assert first["stats"]["upstream_error"]["code"] == "UPSTREAM_TIMEOUT", first["stats"]

    stub.plan = [_job("job_002")]
    second = _post(client)
    assert second["stats"]["sent_new"] == 1, second["stats"]
    assert second["stats"]["upstream_error"], f"内容相同的 chat 失败被吞掉了：{second['stats']}"
    assert second["stats"]["upstream_error"]["code"] == "UPSTREAM_TIMEOUT"
    run = _latest_run(client)
    assert run["stats"]["upstream_error"] == second["stats"]["upstream_error"]

    _assert_offline(cli, sock, boss_cls)


def test_r1_untouched_stale_attribute_is_not_reported(offline):
    """旧属性没被改过（同一个对象）：**不许**误报成本次失败。"""
    client, cli, sock, boss_cls = offline
    stub = _use(client, _Fix9Stub(plan=[_job("job_001")]))
    stale = {
        "command": "detail",
        "code": "JOB_NOT_FOUND",
        "message": "上一阶段遗留，本次调用窗口内没有被改写",
        "normalized": "GONE",
    }
    stub.last_error = stale
    client.app.state.config["quota"]["daily_greet"] = 5

    result = _post(client)
    assert result["ok"] is True, result
    assert result["stats"]["sent_new"] == 1, result["stats"]
    assert result["stats"]["upstream_error"] is None, f"旧属性未改不许误报：{result['stats']}"
    assert stub.last_error is stale, "测试自己的前提：旧对象必须原样留着"

    _assert_offline(cli, sock, boss_cls)


# =========================================================================== #
# R2 · 异常不能跳过已知错误摘要及风险分类
# =========================================================================== #
def test_r2_chat_records_risk_then_raises_still_locks(offline):
    """chat 先写已知 ENVIRONMENT_RISK 再抛异常：摘要+锁都留、账本不变、无异常正文外泄。"""
    client, cli, sock, boss_cls = offline
    stub = _use(client, _Fix9Stub(plan=[_job("job_001"), _job("job_002")]))

    def chat(**kwargs):
        _record(stub, "chat", "ENVIRONMENT_RISK", "synthetic review")
        # 哨兵只放在**异常正文**里：这条路径必须一个字符都不外泄
        raise RuntimeError("synthetic review token=REVIEW_SENTINEL")

    stub.chat = chat
    client.app.state.config["quota"]["daily_greet"] = 1

    result = _post(client)
    stats = result["stats"]

    assert stats["sent_new"] == 1, stats
    assert _ledger(client)[idem.greet_key("job_001")] == "ok", "已成功的新发不许改判"
    assert stats["conversation_warnings"], "异常必须留下可读告警"
    assert stats["upstream_error"], f"异常跳过了已知摘要：{stats}"
    assert stats["upstream_error"]["normalized"] == "RISK_LOCKED", stats
    run = _latest_run(client)
    assert run["stats"]["upstream_error"] == stats["upstream_error"], "摘要必须与 runs 同源"
    assert stats["risk_locked"] is True, f"已知风险族必须置共用锁：{stats}"
    assert client.get("/api/status").json()["risk_locked"] is True

    assert len(stub.greet_calls) == 1, "停轮，不许发第二个岗位"
    assert idem.greet_key("job_002") not in _ledger(client)

    # 无凭据 / 异常正文外泄
    assert "REVIEW_SENTINEL" not in json.dumps(result, ensure_ascii=False), "接口泄漏了异常原文"
    assert "REVIEW_SENTINEL" not in (run.get("error") or ""), "runs.error 泄漏了异常原文"
    assert "REVIEW_SENTINEL" not in json.dumps(run.get("stats") or {}, ensure_ascii=False)

    # 下一次调用在 search 之前就拒绝
    searches_before = len(stub.search_calls)
    again = _post(client)
    assert again["ok"] is False, again
    assert len(stub.search_calls) == searches_before, "锁着的时候不许先搜索"

    _assert_offline(cli, sock, boss_cls)


@pytest.mark.parametrize(
    "code,expect_risk",
    [
        ("ENVIRONMENT_RISK", True),
        ("AUTH_EXPIRED", False),
        ("GREET_LIMIT", False),
    ],
)
def test_r2_known_summary_with_valid_uid_then_sync_raises(offline, monkeypatch, code, expect_risk):
    """chat 给出有效 uid **同时**留下可识别摘要，随后会话落库抛异常：摘要/锁都要保住。"""
    client, cli, sock, boss_cls = offline
    stub = _use(client, _Fix9Stub(plan=[_job("job_001"), _job("job_002")]))

    def chat(**kwargs):
        _record(stub, "chat", code, "synthetic review")
        return [
            {
                "uid": "fake_job_001",
                "job_id": "job_001",
                "hr_name": "张女士",
                "company": "某制造企业",
            }
        ]

    stub.chat = chat
    client.app.state.config["quota"]["daily_greet"] = 1

    def boom_upsert(*args, **kwargs):
        raise RuntimeError("session sync boom token=REVIEW_SENTINEL")

    monkeypatch.setattr(models, "upsert_conversation", boom_upsert, raising=True)

    result = _post(client)
    stats = result["stats"]

    assert stats["sent_new"] == 1, stats
    assert _ledger(client)[idem.greet_key("job_001")] == "ok", "已发账本不变"
    assert stats["conversation_warnings"], "落库异常必须留下可读告警"
    assert stats["upstream_error"], f"落库异常不许跳过已知摘要：{stats}"
    assert stats["upstream_error"]["code"] == code, stats
    run = _latest_run(client)
    assert run["stats"]["upstream_error"] == stats["upstream_error"], "摘要必须与 runs 同源"

    # 风险族置锁，非风险族不伪造（AUTH / LIMIT 只停轮）
    assert stats["risk_locked"] is expect_risk, stats
    assert client.get("/api/status").json()["risk_locked"] is expect_risk

    assert len(stub.greet_calls) == 1, "停轮，不重试、不发下一个"
    assert idem.greet_key("job_002") not in _ledger(client)
    # 不伪造"同步完成"的证据
    assert not _conversation_exists(client, "fake_job_001"), "落库失败不许留下半截会话"

    assert "REVIEW_SENTINEL" not in json.dumps(result, ensure_ascii=False)
    assert "REVIEW_SENTINEL" not in (run.get("error") or "")

    _assert_offline(cli, sock, boss_cls)
