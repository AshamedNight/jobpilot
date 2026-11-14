"""搜索直发（`POST /api/run/direct-greet`）—— 5 组（任务书 §3）。

这一批的口径与 `sweep` 不同：**不做 detail、不做评分、不调 LLM**，搜索回来直接发固定消息。
所以本文件把这三个入口全部换成"一碰就失败"的探针：

- `matcher.score` —— 低分/无正文岗位也要能发，任何评分调用都是越界
- `compose_opener` —— 固定消息，不许走模板/LLM
- `llm_gateway.writer_for` —— 同上
- 桩的 `detail()` —— 本批完全不碰详情

同时复用 fix6 的离线硬护栏（真实 Boss CLI / 真适配器类 / socket 连接），
临时库 + 本地桩，零网络、零真实发送。
"""

from __future__ import annotations

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
from app.core import state_machine as sm
from app.main import create_app

#: 【闭环 1】改造前的那句固定文案。现在只用来断言"它已经不再出现"。
#: 真机数据：11 条招呼 77 秒发完、一字不差，是平台行为风控的直接诱因。
LEGACY_FIXED_MESSAGE = "您好，我对这个岗位感兴趣，希望进一步了解岗位情况。"

_BY_ID = {str(job["job_id"]): dict(job) for job in FAKE_JOBS}


def _job(job_id: str, **overrides) -> dict:
    """从剧本复制一条岗位，可覆盖字段（用于造"缺 ID"的脏数据）。"""
    item = dict(_BY_ID[job_id])
    item.update(overrides)
    return item


class _DirectStub(FakeAdapter):
    """直发桩：`search` 按剧本返回，`detail` 一碰就炸，`greet`/`chat` 可注入结果。"""

    def __init__(self, plan=None, greet_script=None, chat_script=None, search_script=None):
        super().__init__()
        self.plan = [dict(item) for item in (plan if plan is not None else [_job("job_001")])]
        self._greet_script = list(greet_script or [])
        self._chat_script = list(chat_script or [])
        self._search_script = list(search_script or [])
        self.detail_calls: list[str] = []
        self.greet_calls: list[tuple[str, str, str]] = []
        self.chat_calls: list[str] = []
        self.last_error = None

    def probe(self) -> set[str]:
        self.calls.append("probe")
        return set(self.capabilities)

    def search(self, query: str = "", **filters) -> list[dict]:
        self.calls.append(f"search:{query}")
        if self._search_script:
            step = self._search_script.pop(0)
            if step == "raise":
                raise RuntimeError("剧本：搜索直接抛错")
            if step == "risk":
                self.last_error = {"code": "ENVIRONMENT_RISK", "message": "剧本：搜索命中风控"}
                return []
        return [dict(item) for item in self.plan]

    def detail(self, security_id: str, job_id: str | None = None) -> dict:
        self.detail_calls.append(str(job_id))
        raise AssertionError("搜索直发批次不许调用 detail")

    def greet(self, security_id: str, job_id: str, message: str) -> dict:
        self.greet_calls.append((str(security_id), str(job_id), message))
        step = self._greet_script.pop(0) if self._greet_script else None
        if step == "risk":
            return err_result("ACCOUNT_RISK", "剧本：命中风控")
        if step == "limit":
            return err_result("GREET_LIMIT", "剧本：今日额度已用尽")
        if step == "auth":
            return err_result("AUTH_EXPIRED", "剧本：登录态失效")
        if step == "unconfirmed":
            return err_result(None, "剧本：发出但无法确认")
        if step == "already":
            return err_result("ALREADY_GREETED", "剧本：平台早已打过招呼")
        if step == "fail":
            return err_result("UPSTREAM_ERROR", "剧本：普通失败")
        # 成功：登记 uid 供 chat 反查（与 FakeAdapter 同款）
        self._greeted[str(job_id)] = f"fake_{job_id}"
        self._greeting_text[str(job_id)] = message
        return ok_result("剧本：发送成功", raw={"fake": True})

    def chat(self, **filters) -> list[dict]:
        self.chat_calls.append("chat")
        step = self._chat_script.pop(0) if self._chat_script else None
        if step == "empty":
            self.last_error = None
            return []
        if step == "risk":
            self.last_error = {"code": "ENVIRONMENT_RISK", "message": "剧本：反查命中风控"}
            return []
        if step == "auth":
            self.last_error = {"code": "AUTH_EXPIRED", "message": "剧本：反查登录失效"}
            return []
        self.last_error = None
        return super().chat(**filters)


@pytest.fixture()
def offline(tmp_path: Path, monkeypatch):
    """临时库 + 三只离线护栏 + "评分/模板/LLM 一碰就炸"。"""
    import app.adapters.boss as boss_module
    import app.main as main_module

    cli = Tripwire("真实 Boss CLI 入口 boss._cli_entry")
    sock = SocketTripwire()
    boss_cls = Tripwire("真适配器类 app.main.BossAdapter")
    monkeypatch.setattr(boss_module, "_cli_entry", cli)
    monkeypatch.setattr(main_module, "BossAdapter", boss_cls)

    app = create_app(db_path=tmp_path / "direct.db", env_path=tmp_path / "no.env")

    def boom(*args, **kwargs):
        raise AssertionError("搜索直发批次不许调用评分 / 模板 / LLM")

    monkeypatch.setattr(matcher_module, "score", boom, raising=True)
    monkeypatch.setattr(dashboard_module, "compose_opener", boom, raising=True)
    monkeypatch.setattr(app.state.llm_gateway, "writer_for", boom, raising=True)

    with offline_stack(app, sock=sock) as client:
        yield client, cli, sock, boss_cls


def _post(client, limit: int | None = None) -> dict:
    """调手动打招呼接口。`limit=None` 用接口默认值（2026-11-14 起默认 1 条）。"""
    url = "/api/run/direct-greet"
    if limit is not None:
        url += f"?limit={limit}"
    response = client.post(url)
    assert response.status_code == 200, response.text
    return response.json()


def _use(client, stub: FakeAdapter) -> FakeAdapter:
    client.app.state.adapter = stub
    client.app.state.adapter_name = "direct_stub"
    return stub


def _greet_ledger(client) -> dict[str, str]:
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


def _conversation(client, uid: str) -> dict:
    conn = models.connect(client.app.state.db_path)
    try:
        row = models.get_conversation(conn, uid)
        return dict(row) if row else {}
    finally:
        conn.close()


def _preset_claim(client, job_id: str, status: str = "ok") -> None:
    """预置一条历史 greet claim（模拟"前几天/本轮前面已经打过"）。"""
    conn = models.connect(client.app.state.db_path)
    try:
        key = idem.greet_key(job_id)
        assert idem.try_claim(conn, key, "greet", job_id) is True
        idem.finish_claim(conn, key, status, {"code": 0})
    finally:
        conn.close()


def _assert_offline(cli, sock, boss_cls) -> None:
    assert cli.hits == [], f"调用了真实 Boss CLI：{cli.hits}"
    assert sock.hits == [], f"发生了网络连接：{sock.hits}"
    assert boss_cls.hits == [], f"构造了真适配器：{boss_cls.hits}"


# --------------------------------------------------------------------------- #
# 1 · 正常搜索直发：缺 JD / 低分岗位照样发
# --------------------------------------------------------------------------- #
def test_g1_direct_greet_sends_without_detail_scoring_or_llm(offline):
    """低分、硬淘汰、无正文的岗位**都能发** —— 本批不看分数、不看正文。

    `job_006`（0 分）、`job_007`（-999 硬淘汰）、`job_004`（-999）在 sweep 里永远不会被打招呼，
    在这里必须全部发出，否则说明"跳过评分"没有真的做到。
    """
    client, cli, sock, boss_cls = offline
    plan = [
        _job("job_006", jd_text=""),
        _job("job_007", jd_text=""),
        _job("job_004", jd_text=""),
    ]
    stub = _use(client, _DirectStub(plan=plan))
    client.app.state.config["quota"]["daily_greet"] = 5
    client.app.state.config["quota"]["daily_detail"] = 50

    # 批内间隔来自 `pacing.interval_s`（90–240 秒）。测试里必须置 0：
    # 否则 3 条就要真 sleep 两次 2 分钟，整个套件会被拖到超时。
    client.app.state.config["pacing"]["interval_s"] = [0, 0]

    result = _post(client, limit=3)
    assert result["ok"] is True, result
    stats = result["stats"]
    assert stats["found"] == 3
    assert stats["eligible"] == 3
    assert stats["sent_new"] == 3, stats
    assert stats["already_greeted"] == 0
    assert stats["failed"] == 0
    assert stats["unconfirmed"] == 0
    assert stats["risk_locked"] is False
    assert result["run_id"] > 0

    # 文案来自文案池（闭环 1）：不空、不超 60 字、同轮不撞句。
    # 固定文案 `您好，我对这个岗位感兴趣…` 已被删除——真机 11 条一字不差是风控诱因。
    assert len(stub.greet_calls) == 3
    messages = [message for _sid, _jid, message in stub.greet_calls]
    for message in messages:
        assert message.strip(), "文案不能是空串"
        assert len(message) <= 60, f"文案超过 60 字：{message}"
        assert "{" not in message, f"占位符没替换：{message}"
    assert len(set(messages)) == 3, f"同一轮 3 条撞句了：{messages}"
    assert all(m != LEGACY_FIXED_MESSAGE for m in messages), "固定文案不许再出现"
    assert {sid for sid, _jid, _m in stub.greet_calls} == {
        str(job["security_id"]) for job in plan
    }

    # detail / 评分 / LLM 一次都没碰（fixture 里已让它们一碰就炸，这里再显式钉一次）
    assert stub.detail_calls == []
    assert "chat" in stub.chat_calls  # 反查用了 chat，这是允许的

    # claim 与会话可追溯
    ledger = _greet_ledger(client)
    assert len(ledger) == 3
    assert set(ledger.values()) == {"ok"}
    for job in plan:
        conversation = _conversation(client, f"fake_{job['job_id']}")
        assert conversation.get("state") == sm.State.GREETED.value
        assert conversation.get("job_id") == str(job["job_id"])

    _assert_offline(cli, sock, boss_cls)


# --------------------------------------------------------------------------- #
# 2 · 去重与额度：先筛历史 claim 再截日额度
# --------------------------------------------------------------------------- #
def test_g1b_manual_endpoint_defaults_to_single_send(offline):
    """`/api/run/direct-greet` 默认只发 **1 条**（2026-11-14）。

    原来不传 limit，等价于 `limit=None` = **发到当日额度用完**。
    日额度 50、当天已发 8 条时，任何前端（网页按钮、手机 App）点一下就会
    **连发 42 条**，而且没有 `send_gap` 就是连珠炮 —— 这是最典型的脚本形状，
    一次就能把账号送进风控。

    **一个"点一下"的按钮永远不该触发批量发送。**
    """
    client, *_ = offline
    plan = [_job("job_006", jd_text=""), _job("job_007", jd_text=""), _job("job_004", jd_text="")]
    _use(client, _DirectStub(plan=plan))
    client.app.state.config["quota"]["daily_greet"] = 5
    client.app.state.config["pacing"]["interval_s"] = [0, 0]

    result = _post(client)  # 不传 limit，走默认
    stats = result["stats"]
    assert stats["eligible"] == 3, "三个都符合条件"
    assert stats["sent_new"] == 1, f"默认只许发 1 条，实际 {stats['sent_new']}"


def test_g1c_manual_limit_is_respected(offline):
    """显式传 `?limit=2` 才发 2 条 —— 批量发送必须是**有意识的选择**。"""
    client, *_ = offline
    plan = [_job("job_006", jd_text=""), _job("job_007", jd_text=""), _job("job_004", jd_text="")]
    _use(client, _DirectStub(plan=plan))
    client.app.state.config["quota"]["daily_greet"] = 5
    client.app.state.config["pacing"]["interval_s"] = [0, 0]

    result = _post(client, limit=2)
    assert result["stats"]["sent_new"] == 2


def test_g2_dedup_and_quota_shared_ledger_with_sweep(offline):
    """同一轮里同时存在：重复项、缺 job_id、缺 security_id、历史已 claim、额度只剩 1。

    期望：`skipped_invalid=2 / skipped_claimed=1 / eligible=2 / sent_new=1 / skipped_by_quota=1`，
    再跑一轮**一条都不重发**，且账本与 `sweep` 共用同一份 `actions`。
    """
    client, cli, sock, boss_cls = offline
    client.app.state.config["quota"]["daily_greet"] = 2
    _preset_claim(client, "job_004")  # 历史已发送 → 占掉 1 条今日额度

    plan = [
        _job("job_001", jd_text=""),
        _job("job_001", jd_text=""),          # 同轮重复（按 job_id 去重）
        _job("job_002", jd_text="", security_id=""),  # 缺 security_id
        _job("job_003", jd_text=""),          # 下面抹掉 job_id
        _job("job_004", jd_text=""),          # 历史已 claim
        _job("job_005", jd_text=""),          # 唯一新岗位
    ]
    plan[3].pop("job_id")
    stub = _use(client, _DirectStub(plan=plan))

    result = _post(client)
    stats = result["stats"]
    assert result["ok"] is True, result
    assert stats["found"] == 6
    # 口径：缺 security_id(1) + 缺 job_id(1) + 同轮重复(1) = 3（都是"无效列表项"）
    assert stats["skipped_invalid"] == 3, stats
    assert stats["skipped_claimed"] == 1, stats   # job_004
    assert stats["eligible"] == 2, stats          # job_001 + job_005（去重后）
    assert stats["skipped_by_quota"] == 1, stats  # 额度只剩 1（daily_greet=2，已用 1）
    assert stats["sent_new"] == 1, stats
    assert len(stub.greet_calls) == 1

    ledger = _greet_ledger(client)
    assert len(ledger) == 2, "共用账本：历史 1 条 + 本次 1 条"
    assert set(ledger.values()) == {"ok"}
    sent_job = stub.greet_calls[0][1]
    assert f"greet:{sent_job}" in ledger

    # 再跑一轮：全部被 claim 挡住，不重发
    before = len(stub.greet_calls)
    again = _post(client)["stats"]
    assert again["sent_new"] == 0, again
    assert again["skipped_claimed"] >= 1, again
    assert len(stub.greet_calls) == before, "第二轮一条都不许再发"

    _assert_offline(cli, sock, boss_cls)


def test_g2b_in_loop_claim_guard_prevents_send(offline, monkeypatch):
    """预过滤放行、但发送前 `try_claim` 失败（并发/重入）→ 必须跳过，**不得发送**。

    这条专钉任务书 §2 的"执行前逐项 try_claim，失败就跳过，不生成或发送"。
    只靠"历史 claim 预过滤"证明不了它：那是**另一条判据**，把预过滤留着、只拆
    循环内的守卫时，上面那条 g2 依然全绿（实测过），所以必须单独钉。
    """
    client, cli, sock, boss_cls = offline
    _preset_claim(client, "job_001")  # 库里确实有 claim
    stub = _use(client, _DirectStub(plan=[_job("job_001", jd_text="")]))
    client.app.state.config["quota"]["daily_greet"] = 5

    import app.core.idempotency as idem_module

    # 让"预过滤"看不见这条 claim（模拟：候选在预过滤之后、发送之前被别的路径抢占）
    monkeypatch.setattr(idem_module, "get_claim", lambda *args, **kwargs: None)

    stats = _post(client)["stats"]
    assert stats["skipped_claimed"] == 1, stats
    assert stats["sent_new"] == 0, "try_claim 抢不到就不许发"
    assert stub.greet_calls == [], f"抢不到 claim 还发了：{stub.greet_calls}"
    # 原 claim 不被改写、不被清除
    assert _greet_ledger(client)[idem.greet_key("job_001")] == "ok"

    _assert_offline(cli, sock, boss_cls)


# --------------------------------------------------------------------------- #
# 3 · 暂停 / 风险 / 能力缺失 / 搜索失败
# --------------------------------------------------------------------------- #
def test_g3_paused_blocks_before_search(offline):
    client, cli, sock, boss_cls = offline
    stub = _use(client, _DirectStub(plan=[_job("job_001", jd_text="")]))
    client.app.state.paused = True

    result = _post(client)
    assert result["ok"] is False, result
    assert "暂停" in result["error"]
    assert stub.greet_calls == []
    assert not any(call.startswith("search:") for call in stub.calls), "暂停时不许先搜索"
    _assert_offline(cli, sock, boss_cls)


def test_g3_risk_lock_blocks_before_search(offline):
    client, cli, sock, boss_cls = offline
    stub = _use(client, _DirectStub(plan=[_job("job_001", jd_text="")]))
    client.app.state.risk_locked = True

    result = _post(client)
    assert result["ok"] is False, result
    assert "风控" in result["error"]
    assert stub.greet_calls == []
    assert not any(call.startswith("search:") for call in stub.calls), "锁着时不许搜索"
    _assert_offline(cli, sock, boss_cls)


def test_g3_missing_greet_capability_is_rejected(offline):
    client, cli, sock, boss_cls = offline
    stub = _use(client, _DirectStub(plan=[_job("job_001", jd_text="")]))
    stub.capabilities = {"search"}  # 没有 greet

    result = _post(client)
    assert result["ok"] is False, result
    assert "能力" in result["error"] or "greet" in result["error"]
    assert stub.greet_calls == []
    assert not any(call.startswith("search:") for call in stub.calls)
    _assert_offline(cli, sock, boss_cls)


def test_g3_search_failure_sends_nothing(offline):
    client, cli, sock, boss_cls = offline
    stub = _use(client, _DirectStub(plan=[_job("job_001", jd_text="")], search_script=["raise"]))

    result = _post(client)
    assert result["ok"] is False, result
    assert stub.greet_calls == [], "搜索失败后一条都不许发"
    assert result.get("upstream_error") or result.get("error")
    _assert_offline(cli, sock, boss_cls)


# --------------------------------------------------------------------------- #
# 4 · 结果分类与停轮
# --------------------------------------------------------------------------- #
def test_g4_already_greeted_is_not_counted_as_new(offline):
    client, cli, sock, boss_cls = offline
    stub = _use(client, _DirectStub(plan=[_job("job_001", jd_text="")], greet_script=["already"]))

    stats = _post(client)["stats"]
    assert stats["sent_new"] == 0, "already 不能冒充新发"
    assert stats["already_greeted"] == 1, stats
    assert stats["failed"] == 0, "already 不是失败"
    assert len(stub.greet_calls) == 1
    _assert_offline(cli, sock, boss_cls)


def test_g4_unconfirmed_stops_round_and_never_retries(offline):
    client, cli, sock, boss_cls = offline
    stub = _use(
        client,
        _DirectStub(
            plan=[_job("job_001", jd_text=""), _job("job_002", jd_text="")],
            greet_script=["unconfirmed"],
        ),
    )

    stats = _post(client)["stats"]
    assert stats["unconfirmed"] == 1, stats
    assert stats["sent_new"] == 0
    assert len(stub.greet_calls) == 1, "结果不确定必须停轮，不许继续下一条"
    assert _greet_ledger(client)[idem.greet_key("job_001")] == "unconfirmed"

    # 再跑：只让桩返回同一条岗位 —— claim 还在，不许重试该岗
    stub.plan = [_job("job_001", jd_text="")]
    again = _post(client)["stats"]
    assert again["sent_new"] == 0
    assert len(stub.greet_calls) == 1, "unconfirmed 的岗位不许自动重试"
    _assert_offline(cli, sock, boss_cls)


def test_g4_risk_locks_and_stops(offline):
    client, cli, sock, boss_cls = offline
    stub = _use(
        client,
        _DirectStub(
            plan=[_job("job_001", jd_text=""), _job("job_002", jd_text="")],
            greet_script=["risk"],
        ),
    )

    stats = _post(client)["stats"]
    assert stats["risk_locked"] is True, stats
    assert stats["failed"] == 1
    assert len(stub.greet_calls) == 1, "风控后必须停轮"
    assert client.get("/api/status").json()["risk_locked"] is True, "必须按现有机制锁风险"
    _assert_offline(cli, sock, boss_cls)


def test_g4_greet_limit_and_auth_stop_round(offline):
    for script in ("limit", "auth"):
        client, cli, sock, boss_cls = offline
        stub = _use(
            client,
            _DirectStub(
                plan=[_job("job_001", jd_text=""), _job("job_002", jd_text="")],
                greet_script=[script],
            ),
        )
        stats = _post(client)["stats"]
        assert stats["failed"] == 1, (script, stats)
        assert len(stub.greet_calls) == 1, f"{script} 必须停轮"
        assert stats["risk_locked"] is (script == "risk")
        _assert_offline(cli, sock, boss_cls)


# --------------------------------------------------------------------------- #
# 5 · 发送后反查失败：已成功的发送不许被改写
# --------------------------------------------------------------------------- #
def test_g5_conversation_lookup_failure_keeps_sent_and_warns(offline):
    client, cli, sock, boss_cls = offline
    stub = _use(
        client,
        _DirectStub(plan=[_job("job_001", jd_text="")], chat_script=["empty"]),
    )

    result = _post(client)
    stats = result["stats"]
    assert stats["sent_new"] == 1, "反查失败不能把已成功的 greet 改判"
    assert _greet_ledger(client)[idem.greet_key("job_001")] == "ok"
    assert stats["conversation_warnings"], "反查失败必须留下告警"
    assert _conversation(client, "fake_job_001") == {}, "uid 未知不许伪造会话"

    # 再跑不重发
    again = _post(client)["stats"]
    assert again["sent_new"] == 0
    assert len(stub.greet_calls) == 1, "claim 已在，不许重发"
    _assert_offline(cli, sock, boss_cls)


def test_g5_conversation_lookup_risk_stops_round(offline):
    client, cli, sock, boss_cls = offline
    stub = _use(
        client,
        _DirectStub(
            plan=[_job("job_001", jd_text=""), _job("job_002", jd_text="")],
            chat_script=["risk"],
        ),
    )

    stats = _post(client)["stats"]
    assert stats["sent_new"] == 1, "第一条已经发出去了，不能因为反查失败改判"
    assert stats["risk_locked"] is True, "反查遇风险同样要停轮并锁定"
    assert len(stub.greet_calls) == 1, "风险之后不许继续下一个岗位"
    _assert_offline(cli, sock, boss_cls)


def test_g9_city_whitelist_blocks_other_cities(offline):
    """【2026-11-12】城市白名单：非目标城市的岗位一条都不许发。

    现场：搜索结果的 `city` 会混入邻近城市，124 条招呼里 **82 条打给了潍坊**。
    打错城市既浪费额度，也是对 HR 的骚扰，而且撤回不了。
    """
    client, cli, sock, boss_cls = offline
    client.app.state.config["quota"]["daily_greet"] = 5
    client.app.state.config["target"]["cities"] = ["淄博"]

    plan = [
        _job("job_001", jd_text="", city="潍坊"),
        _job("job_002", jd_text="", city="淄博"),
    ]
    stub = _use(client, _DirectStub(plan=plan))

    stats = _post(client)["stats"]

    assert stats["skipped_city"] == 1, stats
    assert stats["eligible"] == 1, stats
    assert stats["sent_new"] == 1, stats
    assert [call[1] for call in stub.greet_calls] == ["job_002"], "只许给淄博的岗位发"

    _assert_offline(cli, sock, boss_cls)
