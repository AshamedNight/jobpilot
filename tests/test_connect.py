"""单账号「连接 BOSS」· 离线定向测试（C1–C18）。

依据：`任务书-单账号前端简单连接.md` §4/§5/§7。
全部离线：上游登录/状态/身份三件事都走**注入的替身 runner**，
本文件同时证明"真 CLI 一次都没被碰过"（C18）。

命名边界：本文件是本轮唯一新增测试文件，老 21 个测试文件与 `conftest.py`
逐字节不动（交付时给 `cmp` 原文）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

#: 本机回环基准地址：Host / Origin 校验按它放行（不给测试开后门，
#: 测试就用真实的回环 Host，和浏览器从 127.0.0.1:8010 打开时一模一样）。
LOCAL_BASE = "http://127.0.0.1:8010"

def _cred_path(cred) -> Path:
    """【F1】真实上游布局：凭据在 `<cred_dir>/auth/session.enc`，不是根目录。

    `auth/manager.py:30-34` → `auth_dir = data_dir / "auth"`；`auth/token_store.py:20-26`
    → `session.enc` / `salt` / `refresh.lock` 都在 `auth/` 下。
    fixture 跟着走真实布局，否则测的仍是"错文件受保护"。
    """
    target = Path(cred) / "auth" / "session.enc"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


#: 含敏感含义的字符串：出现在任何响应体里都算泄漏。
_FORBIDDEN_SUBSTRINGS = (
    "session.enc",
    ".boss-agent",
    "cookie",
    "stoken",
    "wt2",
    "secret.key",
    "13800000000",  # 替身身份里的稳定字段（真机上就是手机号那一类）
    "C:\\",
    "/home/",
)


class StubRunner:
    """替身上游：登录 / 本地状态 / 身份三件事都可编排，并计数。"""

    def __init__(
        self,
        *,
        login_result: dict | None = None,
        login_exc: BaseException | None = None,
        local_status: dict | None = None,
        identity: str | None = "stable-identity-A",
        on_login=None,
        block: threading.Event | None = None,
    ) -> None:
        self.login_result = login_result
        self.login_exc = login_exc
        self._local_status = local_status
        self.identity = identity
        self.on_login = on_login
        self.block = block
        self.login_calls = 0
        self.status_calls = 0
        self.identity_calls = 0
        self.last_login_kwargs: dict = {}

    def login(self, *, cred_dir: Path, budget_dir: Path, cdp_url: str, timeout_s: int) -> dict:
        self.login_calls += 1
        self.last_login_kwargs = {
            "cred_dir": Path(cred_dir),
            "budget_dir": Path(budget_dir),
            "cdp_url": cdp_url,
            "timeout_s": timeout_s,
        }
        if self.block is not None:
            assert self.block.wait(5.0), "测试自己没放开阻塞"
        if self.on_login is not None:
            self.on_login(Path(cred_dir))
        if self.login_exc is not None:
            raise self.login_exc
        result = self.login_result if self.login_result is not None else {"ok": True, "data": {"message": "登录成功（cdp）"}}
        # 成功的登录**才**落盘（模拟上游 TokenStore.save()）；失败/超时不写
        if isinstance(result, dict) and result.get("ok") is not False and self.on_login is None:
            _cred_path(cred_dir).write_bytes(b"NEW-SESSION-BYTES")
        return result

    def local_status(self, *, cred_dir: Path) -> dict | None:
        self.status_calls += 1
        if self._local_status is not None:
            return self._local_status
        return {"ok": True, "data": {"logged_in": True}}

    def identity_source(self, *, cred_dir: Path) -> str | None:
        self.identity_calls += 1
        return self.identity


@pytest.fixture()
def dirs(tmp_path: Path, monkeypatch):
    """凭据目录 / 共享预算目录 / 数据目录三者分开（真机就是这个形状）。"""
    cred = tmp_path / "cred-new"
    budget = tmp_path / "budget-shared"
    cred.mkdir()
    budget.mkdir()
    monkeypatch.setenv("JOBPILOT_BOSS_CRED_DIR", str(cred))
    monkeypatch.setenv("JOBPILOT_BOSS_BUDGET_DIR", str(budget))
    return {"cred": cred, "budget": budget, "root": tmp_path}


def make_client(dirs, runner: StubRunner, db_path: Path | None = None) -> TestClient:
    app = create_app(db_path=db_path or (dirs["root"] / "jobpilot-connect.db"), connect_runner=runner)
    # 用真实回环 base_url：Host 校验走真实路径，不为测试放宽
    client = TestClient(app, base_url=LOCAL_BASE)
    client.__enter__()
    return client


def wait_idle(client: TestClient, timeout: float = 5.0) -> dict:
    """等连接任务退出（完成 / 失败 / 超时都算退出）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/connect/status").json()
        if not snap["task_active"]:
            return snap
        time.sleep(0.01)
    raise AssertionError(f"连接任务没有在 {timeout}s 内退出：{snap}")


def assert_no_leak(payload) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    for bad in _FORBIDDEN_SUBSTRINGS:
        assert bad not in text, f"响应里出现了敏感串 {bad!r}：{text[:400]}"


# --------------------------------------------------------------------------- #
# C1 初始状态：disconnected，键集固定，不泄漏
# --------------------------------------------------------------------------- #
def test_c1_initial_status_disconnected_and_no_leak(dirs):
    client = make_client(dirs, StubRunner())
    resp = client.get("/api/connect/status")
    assert resp.status_code == 200
    snap = resp.json()
    assert snap["state"] == "disconnected"
    assert snap["task_active"] is False
    assert snap["cancel_supported"] is False
    assert set(snap) == {
        "state",
        "reason",
        "message",
        "updated_at",
        "task_active",
        "cancel_supported",
        "binding",
        "session_preserved",
    }
    assert_no_leak(snap)


# --------------------------------------------------------------------------- #
# C2 正常连接：connecting → connected
# --------------------------------------------------------------------------- #
def test_c2_start_then_connected(dirs):
    runner = StubRunner()
    client = make_client(dirs, runner)
    assert _cred_path(dirs["cred"]).exists() is False

    # 首次连接必须显式确认绑定（fail-closed，不自动绑定）
    resp = client.post("/api/connect/start", json={"confirm_bind": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] in {"connecting", "connected"}

    snap = wait_idle(client)
    assert snap["state"] == "connected", snap
    assert snap["binding"] == "matched"
    assert snap["reason"] == "login_ok"
    assert runner.login_calls == 1
    assert _cred_path(dirs["cred"]).exists() is True
    assert_no_leak(snap)

    # 绑定已存在后，过期重连不需要再确认
    client.post("/api/connect/start", json={})
    again = wait_idle(client)
    assert again["state"] == "connected", again
    assert runner.login_calls == 2


# --------------------------------------------------------------------------- #
# C3 单任务互斥：重复点击不能开第二个登录任务
# --------------------------------------------------------------------------- #
def test_c3_double_click_only_one_login_task(dirs):
    gate = threading.Event()
    runner = StubRunner(block=gate)
    client = make_client(dirs, runner)

    first = client.post("/api/connect/start", json={"confirm_bind": True})
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "connecting"

    second = client.post("/api/connect/start", json={})
    assert second.status_code == 409, second.text
    assert "已有连接任务" in second.json()["detail"]

    # 第三次也一样（不是"第二次恰好被挡"）
    assert client.post("/api/connect/start", json={}).status_code == 409

    # 【闭环 0 · 2026-11-09】不能"立刻断言"：接口返回 connecting 时，工作线程还在做
    # 登录前的准备（凭据快照、任务记录、备份，都是落盘 IO），到 `runner.login` 实测约 15ms。
    # 原来的即时断言在慢盘（Defender 扫盘）上必红、在快机器上偶绿 —— 这条用例要证明的是
    # "只起了一个登录任务"，不是"立刻起了"。
    deadline = time.monotonic() + 5.0
    while runner.login_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.login_calls == 1

    gate.set()
    assert wait_idle(client)["state"] == "connected"
    assert runner.login_calls == 1


# --------------------------------------------------------------------------- #
# C4 超时与失败必须是两个不同状态
# --------------------------------------------------------------------------- #
def test_c4_login_timeout_state(dirs):
    client = make_client(dirs, StubRunner(login_exc=TimeoutError("upstream timeout")))
    client.post("/api/connect/start", json={})
    snap = wait_idle(client)
    assert snap["state"] == "timeout", snap
    assert "timeout" in snap["reason"]


def test_c5_login_failure_state(dirs):
    client = make_client(
        dirs,
        StubRunner(login_result={"ok": False, "error": {"code": "AUTH_REQUIRED", "message": "未登录"}}),
    )
    client.post("/api/connect/start", json={})
    snap = wait_idle(client)
    assert snap["state"] == "failed", snap
    assert snap["state"] != "timeout"


# --------------------------------------------------------------------------- #
# C6 登录失败 / 半写：旧 session.enc 必须字节级保回来
# --------------------------------------------------------------------------- #
def _seed_session(cred: Path) -> bytes:
    original = b"OLD-SESSION-BYTES-v1"
    _cred_path(cred).write_bytes(original)
    return original


def test_c6_failed_login_keeps_old_session_bytes(dirs):
    original = _seed_session(dirs["cred"])

    def corrupt_then_fail(cred: Path) -> None:
        _cred_path(cred).write_bytes(b"HALF-WRITTEN-GARBAGE")

    runner = StubRunner(
        login_result={"ok": False, "error": {"code": "LOGIN_CREDENTIAL_EXTRACTION_FAILED", "message": "失败"}},
        on_login=corrupt_then_fail,
    )
    client = make_client(dirs, runner)
    client.post("/api/connect/start", json={})
    snap = wait_idle(client)
    assert snap["state"] == "failed", snap
    assert snap["session_preserved"] is True
    # 断言的是字节，不是"文件存在"
    assert _cred_path(dirs["cred"]).read_bytes() == original


def test_c7_exception_during_login_rolls_back_old_session(dirs):
    original = _seed_session(dirs["cred"])

    def corrupt(cred: Path) -> None:
        _cred_path(cred).write_bytes(b"")

    client = make_client(dirs, StubRunner(login_exc=RuntimeError("boom-secret-token"), on_login=corrupt))
    client.post("/api/connect/start", json={})
    snap = wait_idle(client)
    assert snap["state"] == "failed", snap
    assert _cred_path(dirs["cred"]).read_bytes() == original
    # 异常正文不进响应，只留类型名
    assert_no_leak(snap)
    assert "boom-secret-token" not in json.dumps(snap, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# C8 账号身份无法确认 = 停（不许显示成已连接）
# --------------------------------------------------------------------------- #
def test_c8_unverifiable_identity_is_not_connected(dirs):
    client = make_client(dirs, StubRunner(identity=None))
    client.post("/api/connect/start", json={})
    snap = wait_idle(client)
    assert snap["state"] == "needs_user_action", snap
    assert snap["reason"] == "account_identity_unverified"
    assert snap["binding"] == "unverified"


def test_c9_identity_mismatch_stops_and_keeps_binding(dirs):
    # 先用身份 A 正常绑一次
    first = make_client(dirs, StubRunner(identity="identity-A"))
    first.post("/api/connect/start", json={"confirm_bind": True})
    assert wait_idle(first)["state"] == "connected"
    binding_file = dirs["root"] / "data" / "connect" / "account_binding.json"
    if not binding_file.exists():  # data_dir = db 所在目录
        binding_file = sorted(dirs["root"].glob("**/account_binding.json"))[0]
    before = binding_file.read_bytes()

    # 换一个账号再来
    second = make_client(dirs, StubRunner(identity="identity-B"))
    second.post("/api/connect/start", json={"confirm_bind": True})
    snap = wait_idle(second)
    assert snap["state"] == "needs_user_action", snap
    assert snap["reason"] == "account_mismatch"
    assert snap["binding"] == "mismatch"
    assert binding_file.read_bytes() == before  # 不覆盖旧绑定、不自动修复


def test_c10_first_connect_requires_explicit_bind(dirs):
    client = make_client(dirs, StubRunner(identity="identity-A"))
    client.post("/api/connect/start", json={})
    snap = wait_idle(client)
    assert snap["state"] == "needs_user_action", snap
    assert snap["reason"] == "account_binding_required"
    assert list(dirs["root"].glob("**/account_binding.json")) == []

    # 用户确认后再连一次 → connected
    client.post("/api/connect/start", json={"confirm_bind": True})
    assert wait_idle(client)["state"] == "connected"


# --------------------------------------------------------------------------- #
# C11 风险锁存在 = 停（503，不是 409）且一次上游调用都不发
# --------------------------------------------------------------------------- #
def test_c11_risk_lock_blocks_without_calling_upstream(dirs):
    (dirs["budget"] / "cdp_risk_lock.json").write_text('{"locked": true}', encoding="utf-8")
    runner = StubRunner()
    client = make_client(dirs, runner)
    resp = client.post("/api/connect/start", json={})
    assert resp.status_code == 503, resp.text
    assert "风险锁" in resp.json()["detail"]
    assert runner.login_calls == 0
    assert client.get("/api/connect/status").json()["state"] == "needs_user_action"


def test_c11b_risk_lock_in_cred_dir_also_blocks(dirs):
    (dirs["cred"] / "cdp_risk_lock.json").write_text("{}", encoding="utf-8")
    runner = StubRunner()
    client = make_client(dirs, runner)
    assert client.post("/api/connect/start", json={}).status_code == 503
    assert runner.login_calls == 0


# --------------------------------------------------------------------------- #
# C12 跨站 / 非回环：403，且不启动任务
# --------------------------------------------------------------------------- #
def test_c12_cross_site_origin_rejected(dirs):
    runner = StubRunner()
    client = make_client(dirs, runner)
    resp = client.post("/api/connect/start", json={}, headers={"origin": "http://evil.example"})
    assert resp.status_code == 403, resp.text
    assert runner.login_calls == 0


def test_c13_non_loopback_host_rejected(dirs):
    runner = StubRunner()
    client = make_client(dirs, runner)
    resp = client.post("/api/connect/start", json={}, headers={"host": "10.0.0.5:8010"})
    assert resp.status_code == 403, resp.text
    assert runner.login_calls == 0


# --------------------------------------------------------------------------- #
# C14 陈旧 connecting 自动过期（内存态不会永远卡死）
# --------------------------------------------------------------------------- #
def test_c14_stale_connecting_expires(dirs):
    """【初审修回】口径收紧：`task_active` 现在表示"有任务占用"。

    租约到期只把**展示状态**改成 `timeout`，旧线程还没退出时占用不放（这才是对的：
    那一刻上游登录可能正在写凭据）。本批之前这里断言 `task_active is False` 并允许
    立刻重开连接 —— 那条期望把缺陷写成了契约，按初审第 2 条修正。
    """
    gate = threading.Event()
    runner = StubRunner(block=gate)
    client = make_client(dirs, runner)
    mgr = client.app.state.connect

    assert client.post("/api/connect/start", json={"confirm_bind": True}).status_code == 200
    with mgr._lock:  # noqa: SLF001 - 测试要制造陈旧态，只能直接改内部时钟
        mgr._deadline = time.monotonic() - 1

    snap = client.get("/api/connect/status").json()
    assert snap["state"] == "timeout", snap
    assert snap["task_active"] is True, "旧线程未退出时不许报空闲"
    assert client.post("/api/connect/start", json={"confirm_bind": True}).status_code == 409

    gate.set()
    wait_idle(client)  # 线程真的退出后占用才释放
    assert client.post("/api/connect/start", json={"confirm_bind": True}).status_code == 200


def test_c15_restart_has_no_residual_state(dirs):
    gate = threading.Event()
    first = make_client(dirs, StubRunner(block=gate))
    first.post("/api/connect/start", json={})
    assert first.get("/api/connect/status").json()["state"] == "connecting"
    gate.set()
    wait_idle(first)

    # 新进程 = 新 manager（同一份目录、同一份库）
    fresh = make_client(dirs, StubRunner())
    snap = fresh.get("/api/connect/status").json()
    assert snap["state"] == "disconnected", snap
    assert snap["task_active"] is False


# --------------------------------------------------------------------------- #
# C16 双向互斥：连接中拒绝业务发送；业务运行中拒绝连接
# --------------------------------------------------------------------------- #
def test_c16_connecting_blocks_business_runs(dirs):
    gate = threading.Event()
    client = make_client(dirs, StubRunner(block=gate))
    client.post("/api/connect/start", json={})
    assert client.get("/api/connect/status").json()["state"] == "connecting"

    resp = client.post("/api/run/direct-greet")
    assert resp.status_code == 409, resp.text
    assert "连接" in resp.json()["detail"]

    gate.set()
    wait_idle(client)


def test_c17_business_run_blocks_connect(dirs):
    runner = StubRunner()
    client = make_client(dirs, runner)
    mgr = client.app.state.connect
    mgr.business_enter()
    try:
        resp = client.post("/api/connect/start", json={})
        assert resp.status_code == 409, resp.text
        assert runner.login_calls == 0
    finally:
        mgr.business_exit()
    assert client.post("/api/connect/start", json={}).status_code == 200
    wait_idle(client)


# --------------------------------------------------------------------------- #
# C18 边界：没有取消端点 / 没有 CORS 通配 / 未登录也能开页面 / 真 CLI 零调用
# --------------------------------------------------------------------------- #
def test_c18a_no_cancel_endpoint(dirs):
    client = make_client(dirs, StubRunner())
    assert client.post("/api/connect/cancel").status_code in {404, 405}
    assert client.get("/api/connect/status").json()["cancel_supported"] is False


def test_c18b_no_cors_wildcard_and_page_works_logged_out(dirs):
    client = make_client(dirs, StubRunner())
    assert "access-control-allow-origin" not in {k.lower() for k in client.get("/").headers}
    assert client.get("/").status_code == 200
    assert client.get("/api/status").status_code == 200
    # 未登录时业务端点仍 fail-closed（fake 适配器下也不会因为"有连接页"就变成可发）
    assert client.get("/api/connect/status").json()["state"] == "disconnected"


def test_c18c_flow_never_touches_real_cli(dirs, monkeypatch):
    import app.api.connect as connect_module

    def _tripwire(*args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("连接流程不许碰真 CLI：这里应是注入的 runner")

    monkeypatch.setattr(connect_module, "_cli_entry", _tripwire)
    client = make_client(dirs, StubRunner())
    client.post("/api/connect/start", json={"confirm_bind": True})
    assert wait_idle(client)["state"] == "connected"


def test_c18d_login_command_uses_explicit_dirs(dirs):
    runner = StubRunner()
    client = make_client(dirs, runner)
    client.post("/api/connect/start", json={"confirm_bind": True})
    wait_idle(client)
    kwargs = runner.last_login_kwargs
    assert kwargs["cred_dir"] == dirs["cred"]
    assert kwargs["budget_dir"] == dirs["budget"]
    assert "127.0.0.1:9222" in kwargs["cdp_url"]
    assert kwargs["timeout_s"] > 0
