"""BossAdapter（P2-A 只读真适配器）—— 10 条，**不打真网**。

全部用 `monkeypatch` 顶掉 `boss._cli_entry`（唯一碰上游的入口），fixture 是
`boss_agent_cli 3.0.1` 的真实信封形状（见 `probe-raw汇总.md` §schema / Step0 §3）。

覆盖补丁 v0.4.1 的六条硬要求：
  §1.1 argv 形状（`--data-dir` 在子命令前 + `--json` + `prog_name` + `standalone_mode`）
  §1.2 `data` 形状异常必须显式报错，不许静默兜底
  §1.4 `chatmsg` 实现（`from` 是名字 / 无 `platform_mid`）
  §1.5 `welfare`(list) / `raw_json`(dict) 落库前必须序列化
  §1.7 `hr_active` 枚举映射（`half-year` → `half_year`）
  以及防误发闸门：写操作必须 `NotImplementedError("P2-B 才实现")`
"""

from __future__ import annotations

import json

import pytest

from app import models
from app.adapters.base import err_result
from app.adapters.boss import BossAdapter, normalize_error_code

# --------------------------------------------------------------------------- #
# 上游信封 fixture（字段照抄 JobItem.to_dict()，见补丁 §1.6 / Step0 §2.1）
# --------------------------------------------------------------------------- #
JOB_ITEM = {
    "job_id": "enc_job_1",
    "title": "法务专员",
    "company": "某科技有限公司",
    "salary": "8-12K",
    "city": "淄博",
    "district": "张店区",
    "experience": "3-5年",
    "education": "本科",
    "skills": ["合同审核"],
    "welfare": ["五险一金", "年终奖"],  # ← list：落库前必须 json.dumps
    "industry": "互联网",
    "scale": "100-499人",
    "stage": "A轮",
    "boss_name": "张**",
    "boss_title": "HR",
    "boss_active": "在线",
    "boss_active_desc": "今日活跃",
    "security_id": "sec_abc",
    "lid": "lid_1",
    "greeted": False,
}

CHAT_ITEM = {
    "name": "张**",
    "title": "法务专员",
    "brand_name": "某科技有限公司",
    "initiated_by": "我主动",
    "last_msg": "您好，方便聊一下吗",
    "last_time": "2026-10-26 10:00",
    "last_ts": 1761469200000,
    "msg_status": "已读",
    "uid": "uid_stable_1",
    "security_id": "sec_rotating",  # ← 敏感，禁止出现在返回值里
    "encrypt_job_id": "enc_job_1",
    "unread": 1,
}

CHATMSG_ITEM = {
    "from": "我",
    "type": "text",
    "text": "您好，看到贵司在招法务专员",
    "time": "2026-10-26 10:00",
    "message_id": "mid_1",
    "timestamp": 1761469200000,
}


class StubCli:
    """顶替上游 `cli`：按顺序吐信封；`ok=false` 时抛 `SystemExit(1)`（上游真实行为）。"""

    def __init__(self, *envelopes: object) -> None:
        self.envelopes = list(envelopes)
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, args=None, prog_name=None, standalone_mode=True) -> None:  # noqa: ANN001
        self.calls.append(list(args or []))
        self.kwargs.append({"prog_name": prog_name, "standalone_mode": standalone_mode})
        envelope = self.envelopes.pop(0) if len(self.envelopes) > 1 else self.envelopes[0]
        if isinstance(envelope, str):
            print(envelope)  # 非 JSON 原文
            raise SystemExit(1)
        print(json.dumps(envelope, ensure_ascii=False))
        if isinstance(envelope, dict) and envelope.get("ok") is False:
            raise SystemExit(1)


def _ok(data, pagination=None) -> dict:
    return {
        "ok": True,
        "schema_version": "1.0",
        "command": "search",
        "data": data,
        "pagination": pagination,
        "error": None,
        "hints": None,
    }


def _err(code: str, message: str = "失败") -> dict:
    return {
        "ok": False,
        "schema_version": "1.0",
        "command": "search",
        "data": None,
        "pagination": None,
        "error": {"code": code, "message": message, "recoverable": False},
        "hints": None,
    }


@pytest.fixture()
def adapter(tmp_path) -> BossAdapter:
    """data_dir 指向 tmp，绝不碰 ~/.boss-agent/。"""
    return BossAdapter({"adapter_options": {"data_dir": str(tmp_path), "timeout_s": 5}})


def _patch_cli(monkeypatch, stub: StubCli) -> None:
    monkeypatch.setattr("app.adapters.boss._cli_entry", lambda: stub)


# --------------------------------------------------------------------------- #
# 1. search 字段映射
# --------------------------------------------------------------------------- #
def test_search_maps_fixture_to_unified_fields(adapter, monkeypatch):
    """列表项 → `jobs` 列对齐的统一字段（含 hr_active 枚举与 welfare 序列化）。"""
    _patch_cli(monkeypatch, StubCli(_ok([JOB_ITEM], {"page": 1, "has_more": False})))

    jobs = adapter.search("法务", cities=["淄博"])

    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_id"] == "enc_job_1"
    assert job["security_id"] == "sec_abc"
    assert job["title"] == "法务专员"
    assert job["company"] == "某科技有限公司"
    assert job["salary"] == "8-12K"
    assert job["city"] == "淄博"
    assert job["industry"] == "互联网"
    assert job["hr_name"] == "张**"
    # 「今日活跃」→ rank 6 → today（不是我方猜测的时间）
    assert job["hr_active"] == "today"
    assert job["company_scale"] == "100-499人"
    # 上游没有任何发布时间字段 → 只能 None，不许伪造
    assert job["publish_time"] is None
    # list 必须序列化成 JSON 文本（jobs.welfare 是 TEXT）
    assert json.loads(job["welfare"]) == ["五险一金", "年终奖"]
    # raw_json 也必须是字符串
    assert isinstance(job["raw_json"], str)
    assert adapter.stats["skipped_no_publish_time"] == 1


def test_search_half_year_maps_to_half_year(adapter, monkeypatch):
    """`half-year`(上游连字符) → `half_year`(项目枚举下划线)（补丁 §1.7）。"""
    item = dict(JOB_ITEM, boss_active_desc="近半年活跃", boss_active="离线")
    _patch_cli(monkeypatch, StubCli(_ok([item], {"page": 1, "has_more": False})))

    jobs = adapter.search("法务")

    assert jobs[0]["hr_active"] == "half_year"


# --------------------------------------------------------------------------- #
# 2. 缺 security_id 丢弃
# --------------------------------------------------------------------------- #
def test_search_drops_items_missing_security_id(adapter, monkeypatch):
    """`job_id` / `security_id` 缺一即丢，并计入 `skipped_no_id`（探针发现 ②）。"""
    no_security = dict(JOB_ITEM, job_id="enc_job_2", security_id="")
    no_job_id = dict(JOB_ITEM, job_id="", security_id="sec_x")
    _patch_cli(
        monkeypatch, StubCli(_ok([JOB_ITEM, no_security, no_job_id], {"page": 1, "has_more": False}))
    )

    jobs = adapter.search("法务")

    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "enc_job_1"
    assert adapter.stats["skipped_no_id"] == 2


# --------------------------------------------------------------------------- #
# 3. 非零退出 → UPSTREAM_ERROR（不抛异常）+ 错误码归一
# --------------------------------------------------------------------------- #
def test_cli_error_exit_becomes_upstream_error(adapter, monkeypatch):
    """上游 `ok=false` + `SystemExit(1)` → `err_result("UPSTREAM_ERROR")`，**不抛异常**。"""
    _patch_cli(monkeypatch, StubCli(_err("NETWORK_ERROR", "网络请求失败")))

    jobs = adapter.search("法务")

    assert jobs == []
    assert adapter.last_error == err_result(
        "UPSTREAM_ERROR", detail="网络请求失败"
    ) or adapter.last_error["error_code"] == "UPSTREAM_ERROR"


def test_normalize_error_code_already_is_ok():
    """`ALREADY_GREETED` / `ALREADY_APPLIED` 必须归一成 `OK`（幂等成功），不是错误。

    归成错误 → 重跑 sweep 会把"上次已成功"记成失败，actions 里冒出假失败。
    """
    assert normalize_error_code("ALREADY_GREETED") == "OK"
    assert normalize_error_code("ALREADY_APPLIED") == "OK"
    # 该特判的其余 5 组
    assert normalize_error_code("GREET_RESULT_UNKNOWN") == "UNCONFIRMED"
    assert normalize_error_code("ACTION_UNCONFIRMED") == "UNCONFIRMED"
    assert normalize_error_code("ACCOUNT_RISK") == "RISK_LOCKED"
    assert normalize_error_code("AUTH_REQUIRED") == "AUTH"
    assert normalize_error_code("GREET_LIMIT") == "LIMIT"
    assert normalize_error_code("JOB_NOT_FOUND") == "GONE"
    # 其余 43 个 + 未知 → UPSTREAM_ERROR
    assert normalize_error_code("COMPLIANCE_BLOCKED") == "UPSTREAM_ERROR"
    assert normalize_error_code(None) == "UPSTREAM_ERROR"


# --------------------------------------------------------------------------- #
# 4. 输出非 JSON → UPSTREAM_ERROR（不抛异常）
# --------------------------------------------------------------------------- #
def test_non_json_output_becomes_upstream_error(adapter, monkeypatch):
    """上游 stdout 不是合法 JSON → `UPSTREAM_ERROR`，不抛异常。"""
    _patch_cli(monkeypatch, StubCli("<html>被拦截了</html>"))

    jobs = adapter.search("法务")

    assert jobs == []
    assert adapter.last_error is not None
    assert adapter.last_error["error_code"] == "UPSTREAM_ERROR"
    assert adapter.last_error["sent"] is False


def test_data_shape_anomaly_is_reported_not_silently_swallowed(adapter, monkeypatch):
    """【补丁 §1.2】`data` 不是 list → 显式 `UPSTREAM_ERROR`，**不许静默兜底**。

    静默兜底会让每条 job 变成 `{"job_id": None}`，全被 skipped_no_id 丢弃，
    最后 `found=0` 而没有任何人知道是形状错了。
    """
    _patch_cli(monkeypatch, StubCli(_ok({"unexpected": "形如 dict 但不是 items"})))

    jobs = adapter.search("法务")

    assert jobs == []
    assert adapter.last_error["error_code"] == "UPSTREAM_ERROR"
    assert "形状异常" in (adapter.last_error["detail"] or "")


# --------------------------------------------------------------------------- #
# 5. 防误发闸门
# --------------------------------------------------------------------------- #
def test_exchange_implemented_but_batch_greet_still_blocked(adapter):
    """【2026-11-11 更新】闸门口径变了，用例跟着改。

    - `exchange` **已实现**（闭环 2.2）：包一层 `boss exchange <uid> --type wechat`。
      真正的"关着"不再靠抛 `NotImplementedError`，而是靠编排层的 `gate.exchange=review`
      —— 单点判定，见 `api/dashboard.py` 的 followup。所以这里断言"能调 + 结构合法"。
    - `batch_greet` 仍然**上游用不了**（无 `--url`，§8），保留抛错。
    """
    result = adapter.exchange("uid_stable_1", type="wechat")
    assert result["sent"] in (True, False), "exchange 必须返回统一写操作结构（G8）"

    with pytest.raises(NotImplementedError, match="batch-greet"):
        adapter.batch_greet("法务", limit=1)


# --------------------------------------------------------------------------- #
# 6. chat 不伪造 last_msg_from、不透传 security_id
# --------------------------------------------------------------------------- #
def test_chat_does_not_fabricate_last_msg_from(adapter, monkeypatch):
    """`chat()` 只给真机存在的字段；**不许**造 `last_msg_from`，**不许**透传 `security_id`。"""
    _patch_cli(monkeypatch, StubCli(_ok([CHAT_ITEM])))

    conversations = adapter.chat()

    assert len(conversations) == 1
    conversation = conversations[0]
    assert conversation["uid"] == "uid_stable_1"
    assert conversation["unread"] == 1
    assert conversation["initiated_by"] == "我主动"
    assert conversation["last_ts"] == 1761469200000
    # 关键的三个"不许"
    assert "last_msg_from" not in conversation
    assert "security_id" not in conversation
    assert "sec_rotating" not in json.dumps(conversations, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 7. chatmsg 映射（补丁 §1.4）
# --------------------------------------------------------------------------- #
def test_chatmsg_maps_direction_and_platform_mid(adapter, monkeypatch):
    """`from == "我"` → `out`；`platform_mid` 优先 `message_id`，缺失时拼稳定值。"""
    reply = {
        "from": "张**",  # 联系人名 → in
        "type": "text",
        "text": "方便发个简历吗",
        "time": "2026-10-26 10:05",
        "timestamp": 1761469500000,
        # 无 message_id → 必须拼出稳定值，不许编假 mid
    }
    _patch_cli(monkeypatch, StubCli(_ok([CHATMSG_ITEM, reply])))

    messages = adapter.chatmsg("uid_stable_1", raw=True)

    assert len(messages) == 2
    us = messages[0]
    assert us["platform_mid"] == "mid_1"
    assert us["direction"] == "out"  # from == "我"
    assert us["content"] == "您好，看到贵司在招法务专员"
    them = messages[1]
    assert them["direction"] == "in"  # from 是名字，不是"我"
    # 无 message_id → 拼 (timestamp, 方向, 内容 sha1 前 12 位) 的稳定值
    assert them["platform_mid"].startswith("1761469500000:in:")
    assert them["platform_mid"] != "1761469500000:in:"
    assert len(them["platform_mid"].split(":")[-1]) == 12


def test_chatmsg_error_returns_empty_without_raising(adapter, monkeypatch):
    """`chatmsg` 非零退出 → 返回 `[]` + `err_result("UPSTREAM_ERROR")`，不抛异常。"""
    _patch_cli(monkeypatch, StubCli(_err("AUTH_REQUIRED", "未登录")))

    messages = adapter.chatmsg("uid_stable_1")

    assert messages == []
    assert adapter.last_error["error_code"] == "AUTH"


# --------------------------------------------------------------------------- #
# 8. welfare / raw_json 落库序列化（补丁 §1.5）
# --------------------------------------------------------------------------- #
def test_welfare_and_raw_json_are_serialized_before_insert(adapter, monkeypatch, conn):
    """`welfare` 是 list、`raw_json` 是 dict → 直接绑参会让 sqlite 抛 `ProgrammingError`。

    这条能挡住"测试绿但真机入库炸"。
    """
    _patch_cli(monkeypatch, StubCli(_ok([JOB_ITEM], {"page": 1, "has_more": False})))
    jobs = adapter.search("法务")

    inserted = models.upsert_jobs(conn, jobs)  # 不许抛 ProgrammingError
    assert inserted == 1

    row = conn.execute("SELECT welfare, raw_json, gap_flags FROM jobs").fetchone()
    assert json.loads(row["welfare"]) == ["五险一金", "年终奖"]
    assert json.loads(row["raw_json"])["job_id"] == "enc_job_1"
    assert row["gap_flags"] is None  # 首次入库未算，旧行为 NULL


# --------------------------------------------------------------------------- #
# 9. _run_cli 的 argv 形状（补丁 §5 验收第 15 条）
# --------------------------------------------------------------------------- #
def test_run_cli_argv_shape(adapter, monkeypatch, tmp_path):
    """`--data-dir` 必须在子命令**前**、是绝对路径；`--json` 必须在；
    `prog_name="boss"`、`standalone_mode=True`（补丁 §1.1）。"""
    stub = StubCli(_ok([], {"page": 1, "has_more": False}))
    _patch_cli(monkeypatch, stub)

    adapter.search("法务", cities=["淄博"], active="online")

    argv = stub.calls[0]
    assert argv[0] == "--data-dir"
    assert argv[1] == str(tmp_path)  # 绝对路径
    assert argv[2] == "--json"
    assert argv[3] == "search"  # 子命令在全局选项之后
    assert "--active" in argv and "online" in argv
    assert stub.kwargs[0] == {"prog_name": "boss", "standalone_mode": True}


# --------------------------------------------------------------------------- #
# 10. ADAPTER=boss 接线（补丁 §1.3）
# --------------------------------------------------------------------------- #
def test_adapter_env_selects_boss(tmp_path, monkeypatch):
    """`ADAPTER=boss` → `/api/status.adapter == "boss"` 且实例是 `BossAdapter`。

    之前只支持 fake：环境变量被**静默忽略**（仍走 fake、还打假招呼），
    或直接 StartupError 起不来——真机验收根本没法做。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("ADAPTER", "boss")
    # 不打上游：probe 只调 schema，这里顶掉 CLI 入口与版本
    _patch_cli(monkeypatch, StubCli(_ok({"name": "boss-agent-cli"})))
    monkeypatch.setattr(BossAdapter, "upstream_version", staticmethod(lambda: "3.0.1"))

    app = create_app(db_path=tmp_path / "boss.db", env_path=tmp_path / "no.env")
    with TestClient(app) as client:
        assert isinstance(app.state.adapter, BossAdapter)
        status = client.get("/api/status").json()
        assert status["adapter"] == "boss"
        assert status["adapter_class"] == "BossAdapter"
        assert status["upstream_version"] == "3.0.1"
        # 【§11.1 E2】闸门前移到 "exchange 不许出现"：greet 本批真开了（§7），
        # exchange 归 P2-D，在 P2-D 之前它一直不该出现在 capabilities 里。
        assert "exchange" not in status["capabilities"]
        assert status["capabilities"] == ["chat", "detail", "greet", "search"]


def test_unknown_adapter_raises_startup_error(tmp_path, monkeypatch):
    """其他值 → `StartupError`（中文），不许静默回落到 fake。"""
    from app.config import StartupError
    from app.main import create_app

    monkeypatch.setenv("ADAPTER", "selenium")
    with pytest.raises(StartupError, match="不支持的 adapter"):
        create_app(db_path=tmp_path / "x.db", env_path=tmp_path / "no.env")


# --------------------------------------------------------------------------- #
# 11. 【F1】有 --url 时不许再传 --city
# --------------------------------------------------------------------------- #
def test_search_with_url_does_not_pass_city(tmp_path, monkeypatch):
    """【F1】有 `search_url` 时 argv 不许出现 `--city`。

    上游 `CITY_CODES` 只有 40 个城市，淄博/潍坊都不在表内 → `INVALID_PARAM: 未知城市`
    → 被 `if not ok: break` 静默吞成 `found=0`，而且**与登录态无关**（城市校验优先
    于登录检查）。`search_url` 里已经带了 `city` 参数。
    """
    url = "https://www.zhipin.com/web/geek/jobs?city=101120300&query=%E6%B3%95%E5%8A%A1"
    adapter = BossAdapter(
        {"adapter_options": {"data_dir": str(tmp_path)}, "search_url": url}
    )
    stub = StubCli(_ok([], {"page": 1, "has_more": False}))
    _patch_cli(monkeypatch, stub)

    adapter.search("法务", cities=["淄博"], active="online")

    argv = stub.calls[0]
    assert "--url" in argv
    assert argv[argv.index("--url") + 1] == url
    assert "--city" not in argv, "有 --url 时不许传 --city（上游城市表无淄博）"

    # 对照：search_url 为空时，--city 必须照传（不能为了绕 bug 把城市过滤丢掉）
    plain = BossAdapter({"adapter_options": {"data_dir": str(tmp_path)}})
    stub2 = StubCli(_ok([], {"page": 1, "has_more": False}))
    _patch_cli(monkeypatch, stub2)
    plain.search("法务", cities=["淄博"], active="online")
    assert "--city" in stub2.calls[0]
    assert stub2.calls[0][stub2.calls[0].index("--city") + 1] == "淄博"


# --------------------------------------------------------------------------- #
# 12. 【F2】上游失败必须留痕，不许静默
# --------------------------------------------------------------------------- #
def test_upstream_failure_is_recorded_not_silent(adapter):
    """【F2】`search` 失败时 `last_error` 必须带 `code`/`message`，且不抛异常。

    `found=0` 有四种不同原因（没登录 / 参数非法 / 被风控 / 真的没岗位），
    不记下来用户只能看到一个 0。
    """

    def failing(*args: str) -> dict:
        # 只返回上游真实信封；留痕由 `search()` 的失败分支自己调 `_record_upstream_failure`
        return {
            "ok": False,
            "data": None,
            "error": {"code": "INVALID_PARAM", "message": "未知城市: 淄博"},
            "hints": None,
        }

    adapter._run_cli = failing  # type: ignore[method-assign]

    jobs = adapter.search("法务", cities=["淄博"])  # 不许抛异常

    assert jobs == []
    assert adapter.last_error is not None
    assert adapter.last_error["code"] == "INVALID_PARAM"
    assert adapter.last_error["message"] == "未知城市: 淄博"
    assert adapter.last_error["command"] == "search"
    assert adapter.last_error["normalized"] == "UPSTREAM_ERROR"
    # 兼容既有断言：err_result 的键仍在
    assert adapter.last_error["sent"] is False
    assert adapter.last_error["error_code"] == "UPSTREAM_ERROR"


# --------------------------------------------------------------------------- #
# 13. 【F4】上游版本不符必须进 warnings
# --------------------------------------------------------------------------- #
def test_upstream_version_mismatch_raises_warning(tmp_path, monkeypatch):
    """【F4】版本不符 → `/api/status.warnings` 出现含实际版本的条目；相符则没有。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    _patch_cli(monkeypatch, StubCli(_ok({"name": "boss-agent-cli"})))

    # 不符
    monkeypatch.setenv("ADAPTER", "boss")
    monkeypatch.setattr(BossAdapter, "upstream_version", staticmethod(lambda: "9.9.9"))
    app = create_app(db_path=tmp_path / "v1.db", env_path=tmp_path / "no.env")
    with TestClient(app) as client:
        warnings = client.get("/api/status").json()["warnings"]
        assert any("9.9.9" in str(w) for w in warnings), "版本不符必须告警"
        assert any("3.0.1" in str(w) for w in warnings)

    # 相符
    monkeypatch.setattr(BossAdapter, "upstream_version", staticmethod(lambda: "3.0.1"))
    app2 = create_app(db_path=tmp_path / "v2.db", env_path=tmp_path / "no.env")
    with TestClient(app2) as client:
        warnings2 = client.get("/api/status").json()["warnings"]
        assert not any("9.9.9" in str(w) for w in warnings2), "版本相符不该告警"


# --------------------------------------------------------------------------- #
# 【P2-B §11 第 9–12 条】detail 失败收口 + greet 真实现
# --------------------------------------------------------------------------- #
def _detail_ok(description: str = "岗位职责：审核合同") -> dict:
    return {
        "ok": True,
        "schema_version": "1.0",
        "command": "detail",
        "data": {
            "job_id": "enc_job_1",
            "title": "法务专员",
            "company": "某科技有限公司",
            "salary": "8-12K",
            "city": "淄博",
            "experience": "3-5年",
            "education": "本科",
            "description": description,
            "address": "张店区某路",
            "skills": ["合同审核"],
            "boss_name": "张**",
            "boss_title": "HR",
            "boss_active": "今日活跃",
            "security_id": "sec_abc",
            "raw_job_type": "全职",
            "employment_type": "社招",
            "days_per_week": 5,
            "least_month": 12,
            "pay_type": "月薪",
            "greeted": False,
        },
        "pagination": None,
        "error": None,
        "hints": None,
    }


def _greet_ok() -> dict:
    """补页 §B：成功信封 `data = {security_id, job_id, message}`，**没有 uid**。"""
    return {
        "ok": True,
        "schema_version": "1.0",
        "command": "greet",
        "data": {
            "security_id": "sec_abc",
            "job_id": "enc_job_1",
            "message": "打招呼成功",
        },
        "pagination": None,
        "error": None,
        "hints": {"next_actions": ["chat"]},
    }


def _cmd_err(code: str, message: str = "失败", command: str = "greet") -> dict:
    return {
        "ok": False,
        "schema_version": "1.0",
        "command": command,
        "data": None,
        "pagination": None,
        "error": {"code": code, "message": message, "recoverable": False},
        "hints": None,
    }


def test_detail_failure_uses_record_upstream_failure(adapter, monkeypatch):
    """P2-A 的 P3 收口：`detail` 失败时 `last_error` 四字段**都有值**，不是三个 None。"""
    _patch_cli(monkeypatch, StubCli(_cmd_err("JOB_NOT_FOUND", "职位不存在或已下架", "detail")))

    result = adapter.detail("sec_abc", "enc_job_1")

    assert result["ok"] is False
    error = adapter.last_error
    assert error["command"] == "detail"          # ← 原来这里是 None
    assert error["code"] == "JOB_NOT_FOUND"      # ← 原来这里是 None
    assert error["message"] == "职位不存在或已下架"  # ← 原来这里是 None
    assert error["normalized"] == "GONE"
    # `err_result` 的原有四键仍在（不二选一，测试与页面读同一份结构）
    assert error["sent"] is False


def test_greet_success_maps_to_sent(adapter, monkeypatch):
    """成功信封 → `sent=True`、`raw` 只有 `{"code": 0}`、`already_greeted=False`。"""
    stub = StubCli(_greet_ok())
    _patch_cli(monkeypatch, stub)

    result = adapter.greet("sec_abc", "enc_job_1", "您好，看到贵司在招法务专员")

    assert result["sent"] is True
    assert result["error_code"] is None
    assert result["raw"] == {"code": 0}
    assert result["already_greeted"] is False

    # argv 形状：全局选项在前、`--json` 紧随、单发**没有** `--count`
    argv = stub.calls[-1]
    assert argv[0] == "--data-dir"
    assert argv[2] == "--json"
    assert argv[3:] == [
        "greet",
        "sec_abc",
        "enc_job_1",
        "--message",
        "您好，看到贵司在招法务专员",
    ]
    assert "--count" not in argv


def test_greet_already_greeted_normalized_ok(adapter, monkeypatch):
    """`ALREADY_GREETED`（上游第二层去重）→ 归一为 `OK`、`sent=True`、`already_greeted=True`。

    **不许当失败**：归成错误会让重跑 sweep 把"上次已成功"记成 `actions` 里的假失败。
    """
    _patch_cli(monkeypatch, StubCli(_cmd_err("ALREADY_GREETED", "已向该招聘者打过招呼")))

    result = adapter.greet("sec_abc", "enc_job_1", "您好")

    assert result["sent"] is True
    assert result["error_code"] is None
    assert result["already_greeted"] is True
    assert result["raw"] == {"code": 0}


def test_greet_raw_has_no_security_id(adapter, monkeypatch):
    """冻结清单第 15 条：`raw` 不许出现 `security_id`（上游**不会**替我们脱敏）。"""
    _patch_cli(monkeypatch, StubCli(_greet_ok()))
    result = adapter.greet("sec_abc", "enc_job_1", "您好")
    dumped = json.dumps(result, ensure_ascii=False)
    assert "sec_abc" not in dumped
    assert "security_id" not in dumped
    # 白名单键就这一个
    assert set(result["raw"]) == {"code"}

    # 失败信封同样不许把上游 error.details 整包漏出来
    _patch_cli(monkeypatch, StubCli(_cmd_err("HOOK_BLOCKED", "被 hook 拦下")))
    failed = adapter.greet("sec_abc", "enc_job_1", "您好")
    assert "sec_abc" not in json.dumps(failed, ensure_ascii=False)
    assert failed["sent"] is False
    assert failed["error_code"] == "HOOK_BLOCKED"  # 保留原始码，不糊成 UPSTREAM_ERROR


def test_search_with_url_must_not_pass_positional_query(monkeypatch):
    """【2026-11-12】有 `--url` 时不许再传位置参数 query。

    上游 `commands/search.py:158` 是 `query = query or parsed_url.query` —— 位置参数优先，
    URL 里的 query 被丢掉。真机后果：8 个关键词全在搜"法务"，不同 URL 返回同一批 10 条，
    多关键词轮转形同虚设，池子几天就被打空。
    """
    from app.adapters import boss as B

    adapter = B.BossAdapter.__new__(B.BossAdapter)
    adapter.pages = 1
    adapter.active = "today"
    adapter.search_url = None
    adapter.stats = {}
    adapter.parse_error = None
    calls: list[tuple] = []
    adapter._run_cli = lambda *args: (calls.append(args), {"ok": True, "data": {"jobList": []}})[1]

    url = "https://www.zhipin.com/web/geek/jobs?city=101120600&query=%E5%90%88%E8%A7%84"
    adapter.search("法务", cities=[], page=1, url=url)

    assert calls, "必须真的调了上游"
    argv = calls[0]
    assert "--url" in argv and url in argv
    assert "法务" not in argv, "位置参数 query 会覆盖 URL 里的关键词"
