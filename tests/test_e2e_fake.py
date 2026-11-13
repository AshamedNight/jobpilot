"""Phase 1「e2e 批」全链路（任务书-Phase1-e2e批 §4）—— 4 条。

全部跑在 `FakeAdapter` 上：零网络、零浏览器、零真实 LLM。
每条测试独立 `tmp_path` 数据库，不依赖仓库 `data/`。

覆盖：
  4.1 sweep → followup → exchange → WECHAT_GOT（**必须用岗位 #2 / 律师助理 / 74 分**）
  4.2 `script="risk"` → `RISK_LOCKED`，后续 run 被拒，`/api/pause` 清不掉
  4.3 `script="unconfirmed"` → `EXCHANGE_UNCONFIRMED`，只有 1 条 exchange action、不重试
  4.4 `daily_greet=2` → 只发 2 个招呼，`skipped_by_quota=2`，截断在编排层
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import models
from app.adapters.fake import ONLINE_JOB_IDS
from app.core import state_machine as sm
from app.main import create_app


@pytest.fixture()
def client(tmp_path: Path):
    """每个测试一个独立 app + 独立数据库（`lifespan` 里建表）。"""
    app = create_app(db_path=tmp_path / "e2e.db", env_path=tmp_path / "no.env")
    with TestClient(app) as test_client:
        yield test_client


def _db(client: TestClient):
    """读断言用的连接（路由请求用的是各自短连接，所以这里另开一条）。"""
    return models.connect(client.app.state.db_path)


def _count(conn, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def test_full_chain_sweep_followup_exchange_wechat_got(client: TestClient):
    """4.1 全链路：sweep 打分 + 打招呼 → followup 入库 → 交换 → 拿到微信号。

    岗位 #2（律师助理，74 分）走主动请求交换的 C 方案，**不能用 #1 代替**。
    """
    # gate.exchange 只在测试里临时设 auto，仓库 config.yaml 仍保持 review
    client.app.state.config["gate"]["exchange"] = "auto"

    # ---- sweep -------------------------------------------------------- #
    sweep = client.post("/api/run/sweep").json()
    assert sweep["ok"] is True
    assert sweep["stats"]["found"] == 8
    assert sweep["stats"]["scored"] == 8
    assert sweep["stats"]["greeted"] == 4          # daily_greet 默认 5，4 个过线全打
    assert sweep["stats"]["skipped_by_quota"] == 0

    # ---- 分数精确 82 / 74 / 74 / 74 ------------------------------- #
    jobs = {item["job_id"]: item for item in client.get("/api/jobs").json()["items"]}
    assert len(jobs) == 8
    assert jobs["job_001"]["match_score"] == 82
    assert jobs["job_002"]["match_score"] == 74
    assert jobs["job_003"]["match_score"] == 74
    assert jobs["job_008"]["match_score"] == 74
    for job_id in ("job_004", "job_005", "job_007"):
        assert jobs[job_id]["match_score"] == -999
    # #6 是 0 分（方向未启用），不是硬淘汰
    assert jobs["job_006"]["match_score"] == 0
    assert "方向未启用" in jobs["job_006"]["match_reason"]
    # security_id / raw_json 不进 API
    assert "security_id" not in jobs["job_002"]
    assert "raw_json" not in jobs["job_002"]

    conn = _db(client)
    try:
        # ---- actions：4 条 greet，键是 greet:{job_id} ----------------- #
        greet_keys = sorted(
            row["idempotency_key"]
            for row in conn.execute(
                "SELECT idempotency_key FROM actions WHERE action_type = 'greet'"
            ).fetchall()
        )
        assert greet_keys == ["greet:job_001", "greet:job_002", "greet:job_003", "greet:job_008"]
        # 全部 ok，没有留下 pending
        assert _count(conn, "SELECT COUNT(*) FROM actions WHERE status = 'pending'") == 0
        # 生成开场白 → 每个打招呼一条 llm_calls
        assert _count(conn, "SELECT COUNT(*) FROM llm_calls WHERE purpose = 'opener'") == 4
        assert _count(conn, "SELECT COUNT(*) FROM llm_calls WHERE tokens_in = 0") == 4

        # sweep 后会话都是 GREETED
        states = {
            row["job_id"]: row["state"]
            for row in conn.execute("SELECT job_id, state FROM conversations").fetchall()
        }
        assert states["job_002"] == "GREETED"
        assert len(states) == 4

        # ---- 状态链在状态机里是真有边的（不是手工拼字符串）------------- #
        chain = sm.transition(sm.State.SCORED.value, sm.Event.GREET_SENT.value)
        assert chain == "GREETED"
        chain = sm.transition(chain, sm.Event.HR_REPLIED.value)
        assert chain == "HR_REPLIED"
        chain = sm.transition(chain, sm.Event.EXCHANGE_REQ_OK.value)
        assert chain == "EXCHANGE_REQUESTED"
        chain = sm.transition(chain, sm.Event.EXCHANGE_DONE.value)
        assert chain == "WECHAT_GOT"

        # ---- followup：入库 + 找 #2 的 uid ---------------------------- #
        uid = conn.execute(
            "SELECT uid FROM conversations WHERE job_id = 'job_002'"
        ).fetchone()["uid"]
    finally:
        conn.close()

    followup = client.post("/api/run/followup").json()
    assert followup["ok"] is True
    assert followup["stats"]["wechat_got"] == 1
    assert followup["stats"]["exchanged"] == 1
    assert followup["stats"]["replied"] == 0       # gate.reply=review，本批不发回复

    conn = _db(client)
    try:
        conversation = models.get_conversation(conn, uid)
        assert conversation is not None
        # ---- 状态走到 WECHAT_GOT，号码来源是 chatmsg ------------------ #
        assert conversation["state"] == "WECHAT_GOT"
        assert conversation["wechat_id"] == "lvshi_zhang888"
        assert conversation["wechat_source"] == "chatmsg"
        assert conversation["exchange_agreed_at"]

        # exchange 只有 1 条、且是 ok
        exchange_rows = conn.execute(
            "SELECT idempotency_key, status FROM actions WHERE action_type = 'exchange'"
        ).fetchall()
        assert len(exchange_rows) == 1
        assert exchange_rows[0]["status"] == "ok"
        assert exchange_rows[0]["idempotency_key"] == f"exchange:{uid}"

        # ---- 消息：platform_mid 非空，HR 回复 + 微信号都在 ------------- #
        messages = models.list_messages(conn, uid)
        assert all(message["platform_mid"] for message in messages)
        body = " ".join(str(message["content"] or "") for message in messages)
        assert "加个微信" in body
        assert "lvshi_zhang888" in body
        assert "out" in {message["direction"] for message in messages}
        assert "in" in {message["direction"] for message in messages}
    finally:
        conn.close()

    # ---- 列表不泄露 wechat_id，详情才有 + sensitive=True ------------- #
    listing = client.get("/api/conversations").json()["items"]
    assert listing
    for item in listing:
        assert "wechat_id" not in item
        assert "wechat_source" not in item

    detail = client.get(f"/api/conversations/{uid}").json()
    assert detail["sensitive"] is True
    assert detail["conversation"]["wechat_id"] == "lvshi_zhang888"
    assert detail["messages"]

    # ---- 重复 followup 不重复入库、不重复交换 ------------------------ #
    conn = _db(client)
    try:
        before = _count(conn, "SELECT COUNT(*) FROM messages WHERE uid = ?", (uid,))
    finally:
        conn.close()

    again = client.post("/api/run/followup").json()
    assert again["ok"] is True
    assert again["stats"]["stored_messages"] == 0
    assert again["stats"]["exchanged"] == 0
    assert again["stats"]["wechat_got"] == 0

    conn = _db(client)
    try:
        after = _count(conn, "SELECT COUNT(*) FROM messages WHERE uid = ?", (uid,))
        assert after == before
        assert _count(conn, "SELECT COUNT(*) FROM actions WHERE action_type = 'exchange'") == 1
    finally:
        conn.close()


def test_risk_script_locks_and_blocks_further_runs(tmp_path: Path, monkeypatch):
    """4.2 `script="risk"`：greet 返回 ACCOUNT_RISK → risk_locked，后续 run 全被拒。"""
    monkeypatch.setenv("JOBPILOT_FAKE_SCRIPT", "risk")
    app = create_app(db_path=tmp_path / "risk.db", env_path=tmp_path / "no.env")

    with TestClient(app) as client:
        assert client.get("/api/status").json()["risk_locked"] is False

        sweep = client.post("/api/run/sweep").json()
        assert sweep["ok"] is True          # 这一轮本身跑完（中途停批），不是 500
        assert sweep["stats"]["greeted"] == 0
        assert app.state.risk_locked is True
        assert client.get("/api/status").json()["risk_locked"] is True

        # 后续 sweep / followup 都被拒，错误信息要能看出是风控
        for path in ("/api/run/sweep", "/api/run/followup"):
            blocked = client.post(path).json()
            assert blocked["ok"] is False
            assert ("风控锁定" in blocked["error"]) or ("RISK_LOCKED" in blocked["error"])

        # risk 命中时没有任何成功打招呼 → 不会有会话
        assert client.get("/api/conversations").json()["count"] == 0

        # /api/pause?paused=false 只能清 paused，绝不清 risk_locked
        client.post("/api/pause?paused=true")
        assert client.get("/api/status").json()["paused"] is True
        client.post("/api/pause?paused=false")
        status = client.get("/api/status").json()
        assert status["paused"] is False
        assert status["risk_locked"] is True
        assert app.state.risk_locked is True

        # 失败的那次 greet 记账为 failed，不是 pending
        conn = _db(client)
        try:
            statuses = [
                row["status"]
                for row in conn.execute(
                    "SELECT status FROM actions WHERE action_type = 'greet'"
                ).fetchall()
            ]
            assert statuses == ["failed"]
        finally:
            conn.close()


def test_unconfirmed_exchange_is_recorded_once_and_never_retried(client: TestClient):
    """4.3 `script="unconfirmed"`：exchange 无法确认 → EXCHANGE_UNCONFIRMED，只 1 条、不重试。"""
    client.app.state.config["gate"]["exchange"] = "auto"

    assert client.post("/api/run/sweep").json()["stats"]["greeted"] == 4

    # 换剧本不重建 adapter（该剧本只影响 exchange，greet/search 正常）
    client.app.state.adapter.script = "unconfirmed"

    followup = client.post("/api/run/followup").json()
    assert followup["ok"] is True
    assert followup["stats"]["exchanged"] == 0     # 没确认成功，不算 exchange 完成
    assert followup["stats"]["wechat_got"] == 0

    conn = _db(client)
    try:
        conversation = conn.execute(
            "SELECT uid, state FROM conversations WHERE job_id = 'job_002'"
        ).fetchone()
        uid = conversation["uid"]
        assert conversation["state"] == "EXCHANGE_UNCONFIRMED"

        exchange_rows = conn.execute(
            "SELECT idempotency_key, status FROM actions WHERE action_type = 'exchange'"
        ).fetchall()
        assert len(exchange_rows) == 1
        assert exchange_rows[0]["idempotency_key"] == f"exchange:{uid}"
        assert exchange_rows[0]["status"] == "unconfirmed"
    finally:
        conn.close()

    # 再跑一次 followup：不新增第二条 exchange action，也不再调用 adapter.exchange()
    client.app.state.adapter.calls.clear()
    again = client.post("/api/run/followup").json()
    assert again["ok"] is True
    assert again["stats"]["stored_messages"] == 0

    assert not any(call.startswith("exchange:") for call in client.app.state.adapter.calls), (
        "EXCHANGE_UNCONFIRMED 是终态，不许重试"
    )

    conn = _db(client)
    try:
        assert _count(conn, "SELECT COUNT(*) FROM actions WHERE action_type = 'exchange'") == 1
        assert (
            conn.execute(
                "SELECT state FROM conversations WHERE job_id = 'job_002'"
            ).fetchone()["state"]
            == "EXCHANGE_UNCONFIRMED"
        )
    finally:
        conn.close()


def test_quota_truncates_to_two_greets_in_orchestration(client: TestClient):
    """4.4 `daily_greet=2`：只发 2 个招呼，截断发生在编排层而不是 FakeAdapter 内部。"""
    client.app.state.config["quota"]["daily_greet"] = 2

    sweep = client.post("/api/run/sweep").json()
    assert sweep["ok"] is True
    assert sweep["stats"]["scored"] == 8
    assert sweep["stats"]["greeted"] == 2
    assert sweep["stats"]["skipped_by_quota"] == 2

    conn = _db(client)
    try:
        greet_rows = conn.execute(
            "SELECT idempotency_key FROM actions WHERE action_type = 'greet'"
        ).fetchall()
        assert len(greet_rows) == 2
        # 按分数降序截断：#1（82）先，然后 #2（74，adapter 顺序）
        assert sorted(row["idempotency_key"] for row in greet_rows) == [
            "greet:job_001",
            "greet:job_002",
        ]
    finally:
        conn.close()

    # adapter 只被真的调用了 2 次 greet
    greet_calls = [c for c in client.app.state.adapter.calls if c.startswith("greet:")]
    assert len(greet_calls) == 2

    # 额度进度从配置实时读：剩下 0
    status = client.get("/api/status").json()
    assert status["quota"]["used_today"] == 2
    assert status["quota"]["remaining"] == 0

    # 剧本本身没被改动：仍然 8 个岗位、4 个固定过线岗位
    assert len(client.app.state.adapter.search("法务")) == 8
    assert len(ONLINE_JOB_IDS) == 4


def test_followup_processes_conversation_with_unread_only(client: TestClient):
    """Step 8 回归：真机 chat() 没有 `last_msg_from`，只有 `unread`。

    这是**最糟的失败形态**的守门测试：原始写法
    `if conversation.get("last_msg_from") != "hr": continue`
    在真机上会把每一条会话都静默 continue 掉，而且不报错、`stats` 还全是 0。

    这里直接构造 `{"uid": ..., "unread": 1}`（**不带** `last_msg_from`），
    断言该会话被真的处理（消息入库）。
    """
    # 先走一遍 sweep，让 job_002 有会话（fake 会写 last_msg_from，这里随后顶掉）
    sweep = client.post("/api/run/sweep").json()
    assert sweep["ok"] is True

    adapter = client.app.state.adapter
    conn = _db(client)
    try:
        uid = conn.execute(
            "SELECT uid FROM conversations WHERE job_id = 'job_002'"
        ).fetchone()["uid"]
    finally:
        conn.close()

    # 顶掉 chat()：只给真机存在的字段，**故意不带 last_msg_from**
    adapter.chat = lambda **filters: [
        {"uid": uid, "job_id": "job_002", "hr_name": "李**", "company": "某律所", "unread": 1}
    ]

    followup = client.post("/api/run/followup").json()
    assert followup["ok"] is True
    # 关键断言：会话被处理了（消息入库），而不是被静默跳过
    assert followup["stats"]["conversations"] == 1
    assert followup["stats"]["stored_messages"] > 0, (
        "只有 unread=1、没有 last_msg_from 的会话必须被处理（Step 8）"
    )


def test_followup_skips_conversation_without_any_hr_signal(client: TestClient):
    """反向守门：`unread == 0` 且无 `last_msg_from` → 不处理（不误伤"我主动发起且已读"的会话）。"""
    client.post("/api/run/sweep")

    adapter = client.app.state.adapter
    conn = _db(client)
    try:
        uid = conn.execute(
            "SELECT uid FROM conversations WHERE job_id = 'job_002'"
        ).fetchone()["uid"]
    finally:
        conn.close()

    adapter.chat = lambda **filters: [
        {"uid": uid, "job_id": "job_002", "hr_name": "李**", "company": "某律所", "unread": 0}
    ]
    adapter.calls.clear()

    followup = client.post("/api/run/followup").json()
    assert followup["ok"] is True
    assert followup["stats"]["stored_messages"] == 0
    assert not any(call.startswith("chatmsg:") for call in adapter.calls)
