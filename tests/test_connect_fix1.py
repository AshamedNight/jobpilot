"""单账号连接 · 初审阻塞项修回测试（R1–R9）。

对应初审第 2/3/4/5 条：
- R1–R4 超时分支：旧线程仍在跑时既不许开第二个连接，也不许放行业务发送；
  旧线程迟到返回后不许污染状态 / 凭据 / 账号绑定。
- R5–R6 账号侧失败：身份未确认、首次绑定未确认、账号不匹配 → **一律恢复连接前凭据**；
  连接前没有旧 `session.enc` 时，失败后不许留下可被业务继续使用的新登录态。
- R8 双向互斥按**并发交错**验证（真线程 + 事件编排），不是顺序调用。
- R7 共享预算目录单独存在风险锁 → 503 且一次上游都不碰。

本文件是本轮唯一新增测试文件；既有两个测试文件不改断言（`test_connect.py` 的
c14 仅按"task_active = 有任务占用"的新口径更新两处期望，理由见交付说明 §5）。
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

LOCAL_BASE = "http://127.0.0.1:8010"
OLD_BYTES = b"OLD-SESSION-BYTES-v1"

def _cred_path(cred) -> Path:
    """【F1】真实上游布局：凭据在 `<cred_dir>/auth/session.enc`，不是根目录。

    `auth/manager.py:30-34` → `auth_dir = data_dir / "auth"`；`auth/token_store.py:20-26`
    → `session.enc` / `salt` / `refresh.lock` 都在 `auth/` 下。
    fixture 跟着走真实布局，否则测的仍是"错文件受保护"。
    """
    target = Path(cred) / "auth" / "session.enc"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target



class StubRunner:
    """替身上游：可编排登录/状态/身份，并记录调用次数与顺序。"""

    def __init__(self, *, identity: str | None = "identity-A", local_status: dict | None = None,
                 block: threading.Event | None = None, write_new: bool = True,
                 new_bytes: bytes = b"NEW-SESSION-BYTES") -> None:
        self.identity = identity
        self._local_status = local_status
        self.block = block
        self.write_new = write_new
        self.new_bytes = new_bytes
        self.login_calls = 0
        self.status_calls = 0
        self.identity_calls = 0
        self.login_started = threading.Event()

    def login(self, *, cred_dir: Path, budget_dir: Path, cdp_url: str, timeout_s: int) -> dict:
        self.login_calls += 1
        self.login_started.set()
        if self.block is not None:
            assert self.block.wait(5.0), "测试没放开阻塞"
        # 模拟上游 TokenStore.save()：登录成功就**先**把新凭据写进去
        if self.write_new:
            _cred_path(cred_dir).write_bytes(self.new_bytes)
        return {"ok": True, "data": {"message": "登录成功（cdp）"}}

    def local_status(self, *, cred_dir: Path) -> dict | None:
        self.status_calls += 1
        return self._local_status if self._local_status is not None else {"ok": True, "data": {"logged_in": True}}

    def identity_source(self, *, cred_dir: Path) -> str | None:
        self.identity_calls += 1
        return self.identity


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    cred = tmp_path / "cred-new"
    budget = tmp_path / "budget-shared"
    cred.mkdir()
    budget.mkdir()
    monkeypatch.setenv("JOBPILOT_BOSS_CRED_DIR", str(cred))
    monkeypatch.setenv("JOBPILOT_BOSS_BUDGET_DIR", str(budget))
    return {"cred": cred, "budget": budget, "root": tmp_path}


def make_client(dirs, runner) -> TestClient:
    app = create_app(db_path=dirs["root"] / "connect-fix1.db", connect_runner=runner)
    client = TestClient(app, base_url=LOCAL_BASE)
    client.__enter__()
    return client


def wait_until(predicate, timeout: float = 5.0, what: str = "条件") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"{what} 在 {timeout}s 内没有满足")


def wait_idle(client: TestClient, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/connect/status").json()
        if not snap["task_active"]:
            return snap
        time.sleep(0.01)
    raise AssertionError(f"连接任务没退出：{snap}")


def expire_lease(client: TestClient) -> None:
    """把租约推到过去（模拟"上游一直不返回"的那个时刻）。"""
    mgr = client.app.state.connect
    with mgr._lock:  # noqa: SLF001
        mgr._deadline = time.monotonic() - 1


def seed_session(cred: Path, payload: bytes = OLD_BYTES) -> bytes:
    _cred_path(cred).write_bytes(payload)
    return payload


def binding_files(root: Path) -> list[Path]:
    return list(root.glob("**/account_binding.json"))


# --------------------------------------------------------------------------- #
# R1 超时后旧任务仍在跑：状态是 timeout，但任务仍占用
# --------------------------------------------------------------------------- #
def test_r1_timeout_keeps_old_task_active(dirs):
    gate = threading.Event()
    runner = StubRunner(block=gate, write_new=False)
    client = make_client(dirs, runner)
    assert client.post("/api/connect/start", json={"confirm_bind": True}).status_code == 200
    assert runner.login_started.wait(5.0)

    expire_lease(client)
    snap = client.get("/api/connect/status").json()
    assert snap["state"] == "timeout", snap
    assert snap["task_active"] is True, "旧线程没退出就不能报空闲"
    gate.set()
    wait_idle(client)


# --------------------------------------------------------------------------- #
# R2 超时后第二次连接被拒（旧线程没退出之前）
# --------------------------------------------------------------------------- #
def test_r2_timeout_rejects_second_connect_until_thread_exits(dirs):
    gate = threading.Event()
    runner = StubRunner(block=gate, write_new=False)
    client = make_client(dirs, runner)
    client.post("/api/connect/start", json={"confirm_bind": True})
    assert runner.login_started.wait(5.0)
    expire_lease(client)
    assert client.get("/api/connect/status").json()["state"] == "timeout"

    second = client.post("/api/connect/start", json={"confirm_bind": True})
    assert second.status_code == 409, second.text
    assert runner.login_calls == 1

    gate.set()
    wait_idle(client)
    # 旧线程真的退出后，才允许再连
    assert client.post("/api/connect/start", json={"confirm_bind": True}).status_code == 200
    wait_idle(client)
    assert runner.login_calls == 2


# --------------------------------------------------------------------------- #
# R3 超时后业务发送被拒（旧线程没退出之前）
# --------------------------------------------------------------------------- #
def test_r3_timeout_rejects_business_send(dirs):
    gate = threading.Event()
    runner = StubRunner(block=gate, write_new=False)
    client = make_client(dirs, runner)
    client.post("/api/connect/start", json={"confirm_bind": True})
    assert runner.login_started.wait(5.0)
    expire_lease(client)
    assert client.get("/api/connect/status").json()["state"] == "timeout"

    resp = client.post("/api/run/direct-greet")
    assert resp.status_code == 409, resp.text
    assert "仍未退出" in resp.json()["detail"]

    gate.set()
    wait_idle(client)
    # 线程退出后互斥解除（这里只验"不再是 409"，不验业务结果）
    assert client.post("/api/run/direct-greet").status_code == 200


# --------------------------------------------------------------------------- #
# R4 旧任务迟到成功：不许污染状态 / 凭据 / 绑定
# --------------------------------------------------------------------------- #
def test_r4_late_success_does_not_pollute_state_or_credentials(dirs):
    seed_session(dirs["cred"])
    gate = threading.Event()
    runner = StubRunner(block=gate, identity="identity-A")
    client = make_client(dirs, runner)
    client.post("/api/connect/start", json={"confirm_bind": True})
    assert runner.login_started.wait(5.0)

    expire_lease(client)
    assert client.get("/api/connect/status").json()["state"] == "timeout"
    gate.set()          # 旧线程此刻"迟到成功"
    snap = wait_idle(client)

    assert snap["state"] == "timeout", f"迟到成功不许把状态改成 connected：{snap}"
    assert snap["binding"] == "not_applicable", snap
    assert binding_files(dirs["root"]) == [], "迟到任务不许写账号绑定"
    assert _cred_path(dirs["cred"]).read_bytes() == OLD_BYTES, "迟到任务写的新凭据必须撤回"
    assert snap["session_preserved"] is not False
    # 已放弃的任务不许继续消耗上游：判超时之后 login 之外的只读探测一次都不许发
    assert runner.status_calls == 0, f"迟到任务打了 status {runner.status_calls} 次"
    assert runner.identity_calls == 0, f"迟到任务打了 me {runner.identity_calls} 次"


def test_r4b_late_success_leaves_no_usable_login_when_none_before(dirs):
    gate = threading.Event()
    runner = StubRunner(block=gate, identity="identity-A")
    client = make_client(dirs, runner)
    client.post("/api/connect/start", json={"confirm_bind": True})
    assert runner.login_started.wait(5.0)
    expire_lease(client)
    gate.set()
    snap = wait_idle(client)

    assert snap["state"] == "timeout", snap
    assert not _cred_path(dirs["cred"]).exists(), "连接前没有旧凭据时，失败后不许留下可用登录态"
    assert binding_files(dirs["root"]) == []


# --------------------------------------------------------------------------- #
# R5 登录先写新凭据、随后身份不匹配 → 恢复连接前凭据
# --------------------------------------------------------------------------- #
def test_r5_identity_mismatch_restores_preconnect_credentials(dirs):
    # 先正常绑一次（identity-A），得到"连接前"的凭据与绑定
    first = make_client(dirs, StubRunner(identity="identity-A"))
    first.post("/api/connect/start", json={"confirm_bind": True})
    assert wait_idle(first)["state"] == "connected"
    before_bytes = _cred_path(dirs["cred"]).read_bytes()
    assert before_bytes == b"NEW-SESSION-BYTES"
    binding_before = binding_files(dirs["root"])[0].read_bytes()

    # 换账号再来：登录把**另一份**新凭据写进去了，身份核验才发现不匹配
    # （字节故意与"连接前"不同，否则"已恢复"和"没恢复"是同一个值 = 假绿）
    second = make_client(dirs, StubRunner(identity="identity-B", new_bytes=b"NEW-SESSION-BYTES-B"))
    second.post("/api/connect/start", json={"confirm_bind": True})
    snap = wait_idle(second)

    assert snap["state"] == "needs_user_action", snap
    assert snap["reason"] == "account_mismatch"
    assert snap["session_preserved"] is True
    assert _cred_path(dirs["cred"]).read_bytes() == before_bytes, "必须回到连接前那份，不是本次新写的"
    assert binding_files(dirs["root"])[0].read_bytes() == binding_before


def test_r6_first_bind_unconfirmed_restores_credentials(dirs):
    client = make_client(dirs, StubRunner(identity="identity-A"))
    client.post("/api/connect/start", json={})
    snap = wait_idle(client)
    assert snap["state"] == "needs_user_action", snap
    assert snap["reason"] == "account_binding_required"
    # 连接前没有旧 session.enc → 必须清掉这次登录写进去的那份
    assert not _cred_path(dirs["cred"]).exists()
    assert binding_files(dirs["root"]) == []


def test_r6b_identity_unverified_restores_old_session(dirs):
    seed_session(dirs["cred"])
    client = make_client(dirs, StubRunner(identity=None))
    client.post("/api/connect/start", json={"confirm_bind": True})
    snap = wait_idle(client)
    assert snap["state"] == "needs_user_action", snap
    assert snap["reason"] == "account_identity_unverified"
    assert snap["session_preserved"] is True
    assert _cred_path(dirs["cred"]).read_bytes() == OLD_BYTES


# --------------------------------------------------------------------------- #
# R7 共享预算目录单独有风险锁 → 503，一次上游不碰
# --------------------------------------------------------------------------- #
def test_r7_shared_budget_risk_lock_blocks(dirs):
    (dirs["budget"] / "cdp_risk_lock.json").write_text('{"locked": true}', encoding="utf-8")
    runner = StubRunner()
    client = make_client(dirs, runner)
    resp = client.post("/api/connect/start", json={"confirm_bind": True})
    assert resp.status_code == 503, resp.text
    assert "风险锁" in resp.json()["detail"]
    assert runner.login_calls == 0
    snap = client.get("/api/connect/status").json()
    assert snap["state"] == "needs_user_action" and snap["reason"] == "risk_lock_present"


# --------------------------------------------------------------------------- #
# R8 双向互斥：并发交错（真线程）
# --------------------------------------------------------------------------- #
def test_r8a_concurrent_business_posts_during_connect_are_rejected(dirs):
    gate = threading.Event()
    runner = StubRunner(block=gate, write_new=False)
    client = make_client(dirs, runner)
    client.post("/api/connect/start", json={"confirm_bind": True})
    assert runner.login_started.wait(5.0)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(client.post, "/api/run/direct-greet") for _ in range(3)]
        codes = [f.result(timeout=10).status_code for f in futures]
    assert codes == [409, 409, 409], codes
    assert runner.login_calls == 1

    gate.set()
    wait_idle(client)


def test_r8b_concurrent_connect_during_business_run_is_rejected(dirs):
    runner = StubRunner(identity="identity-A")
    client = make_client(dirs, runner)
    mgr = client.app.state.connect
    started = threading.Event()
    release = threading.Event()

    def blocking_search(*args, **kwargs):
        started.set()
        assert release.wait(10.0)
        return []

    client.app.state.adapter.search = blocking_search  # type: ignore[attr-defined]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        run = pool.submit(client.post, "/api/run/direct-greet")
        assert started.wait(5.0), "业务请求没进到 search"
        wait_until(lambda: mgr._business_inflight > 0, what="业务在跑")  # noqa: SLF001
        resp = client.post("/api/connect/start", json={"confirm_bind": True})
        assert resp.status_code == 409, resp.text
        assert runner.login_calls == 0
        release.set()
        assert run.result(timeout=10).status_code == 200

    assert client.post("/api/connect/start", json={"confirm_bind": True}).status_code == 200
    wait_idle(client)


# --------------------------------------------------------------------------- #
# R9 迟到任务不许把 binding 写进去（与 R4 互补：这次身份不匹配）
# --------------------------------------------------------------------------- #
def test_r9_late_thread_does_not_touch_binding(dirs):
    gate = threading.Event()
    runner = StubRunner(block=gate, identity="identity-B")
    client = make_client(dirs, runner)
    client.post("/api/connect/start", json={"confirm_bind": True})
    assert runner.login_started.wait(5.0)
    expire_lease(client)
    gate.set()
    snap = wait_idle(client)
    assert snap["state"] == "timeout", snap
    assert binding_files(dirs["root"]) == []
    assert not _cred_path(dirs["cred"]).exists()
