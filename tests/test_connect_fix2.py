"""Connect_fix2 · F1 真实凭据布局与快照/恢复边界（T1–T3）。

对应《修单-Phase2-Connect_fix2-安全收口.md》：
- T1 真实 TokenStore 布局与本地 save/load；
- T2 有旧凭据 / 无旧凭据的成功失败与迟到；
- T3 快照读失败 / 备份失败 → login 零调用，不误删原 session。

全部离线、临时目录，不联网、不登录、不发送。
"""

from __future__ import annotations

import json
import os
import concurrent.futures
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.connect import ConnectManager, build_manager
from app.main import create_app

LOCAL_BASE = "http://127.0.0.1:8010"


class StubRunner:
    """替身上游：按真实 auth 布局写/读 `auth/session.enc`，可选写 salt。"""

    def __init__(self, *, ok: bool = True, write_session: bytes | None = b"NEW-SESSION-BYTES",
                 write_salt: bytes | None = b"NEW-SALT", identity: str | None = "identity-A",
                 block: threading.Event | None = None) -> None:
        self.ok = ok
        self.write_session = write_session
        self.write_salt = write_salt
        self.identity = identity
        self.block = block
        self.login_calls = 0
        self.started = threading.Event()

    def login(self, *, cred_dir: Path, budget_dir: Path, cdp_url: str, timeout_s: int) -> dict:
        self.login_calls += 1
        self.started.set()
        if self.block is not None:
            assert self.block.wait(5.0)
        auth = Path(cred_dir) / "auth"
        if self.write_session is not None:
            auth.mkdir(parents=True, exist_ok=True)
            (auth / "session.enc").write_bytes(self.write_session)
        if self.write_salt is not None:
            auth.mkdir(parents=True, exist_ok=True)
            (auth / "salt").write_bytes(self.write_salt)
        if not self.ok:
            return {"ok": False, "error": {"code": "AUTH_REQUIRED", "message": "未登录"}}
        return {"ok": True, "data": {"message": "登录成功（cdp）"}}

    def local_status(self, *, cred_dir: Path) -> dict | None:
        return {"ok": True, "data": {"logged_in": True}}

    def identity_source(self, *, cred_dir: Path) -> str | None:
        return self.identity


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    cred = tmp_path / "cred-root"
    budget = tmp_path / "budget-shared"
    cred.mkdir()
    budget.mkdir()
    monkeypatch.setenv("JOBPILOT_BOSS_CRED_DIR", str(cred))
    monkeypatch.setenv("JOBPILOT_BOSS_BUDGET_DIR", str(budget))
    return {"cred": cred, "budget": budget, "root": tmp_path}


def make_manager(dirs, runner) -> ConnectManager:
    return build_manager(
        {"adapter_options": {"data_dir": str(dirs["budget"])}},
        dirs["root"] / "fix2.db",
        runner=runner,
    )


def make_client(dirs, runner) -> TestClient:
    app = create_app(db_path=dirs["root"] / "fix2.db", connect_runner=runner)
    client = TestClient(app, base_url=LOCAL_BASE)
    client.__enter__()
    return client


def wait_idle(client: TestClient, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/connect/status").json()
        if not snap["task_active"]:
            return snap
        time.sleep(0.01)
    raise AssertionError(f"连接任务没退出：{snap}")


def auth_path(cred: Path, name: str = "session.enc") -> Path:
    return Path(cred) / "auth" / name


# --------------------------------------------------------------------------- #
# T1 真实 TokenStore 布局与本地 save/load（真上游代码，零网络）
# --------------------------------------------------------------------------- #
def test_t1a_manager_uses_real_tokenstore_layout(dirs):
    from boss_agent_cli.auth.token_store import TokenStore

    manager = make_manager(dirs, StubRunner())
    assert manager.auth_dir() == dirs["cred"] / "auth"
    assert manager._session_file() == dirs["cred"] / "auth" / "session.enc"
    assert manager._salt_file() == dirs["cred"] / "auth" / "salt"


def test_t1b_real_tokenstore_roundtrip_matches_our_path(dirs, monkeypatch):
    """真上游 `TokenStore.save/load` 写出来的东西，必须正好落在本项目快照/恢复的位置。"""
    from boss_agent_cli.auth.token_store import TokenStore

    monkeypatch.setenv("BOSS_AGENT_MACHINE_ID", "fix2-offline-test")
    store = TokenStore(dirs["cred"] / "auth")
    store.save({"wt2": "fake", "stoken": "fake"})

    assert auth_path(dirs["cred"], "session.enc").exists()
    assert auth_path(dirs["cred"], "salt").exists()

    manager = make_manager(dirs, StubRunner())
    snapshot = manager._snapshot_credentials()
    assert snapshot["session"] == auth_path(dirs["cred"], "session.enc").read_bytes()
    assert snapshot["salt"] == auth_path(dirs["cred"], "salt").read_bytes()
    # 本地 load 真的能解回来（证明这是"可用凭据"，不是随便一个文件）
    assert store.load()["wt2"] == "fake"


def test_t1c_root_decoy_is_not_credentials(dirs):
    """根目录放个同名文件，不能冒充登录态：判据只看 `auth/session.enc`。"""
    (dirs["cred"] / "session.enc").write_bytes(b"ROOT-DECOY")
    client = make_client(dirs, StubRunner(write_session=None, write_salt=None))
    client.post("/api/connect/start", json={"confirm_bind": True})
    snap = wait_idle(client)
    assert snap["state"] == "failed", snap
    assert snap["reason"] == "session_incomplete"
    # 诱饵原样保留，但从未被当成凭据
    assert (dirs["cred"] / "session.enc").read_bytes() == b"ROOT-DECOY"
    assert not auth_path(dirs["cred"]).exists()


# --------------------------------------------------------------------------- #
# T2 有旧凭据 / 无旧凭据：session + salt 一起按真实路径还原或撤回
# --------------------------------------------------------------------------- #
def test_t2a_old_credentials_session_and_salt_restored(dirs):
    old_session, old_salt = b"OLD-SESSION", b"OLD-SALT"
    auth_path(dirs["cred"]).parent.mkdir(parents=True, exist_ok=True)
    auth_path(dirs["cred"]).write_bytes(old_session)
    auth_path(dirs["cred"], "salt").write_bytes(old_salt)

    client = make_client(dirs, StubRunner(ok=False))
    client.post("/api/connect/start", json={"confirm_bind": True})
    snap = wait_idle(client)

    assert snap["state"] == "failed", snap
    assert snap["session_preserved"] is True
    assert auth_path(dirs["cred"]).read_bytes() == old_session
    assert auth_path(dirs["cred"], "salt").read_bytes() == old_salt, "salt 也必须逐字节还原"


def test_t2b_no_old_credentials_removes_new_session_and_salt(dirs):
    client = make_client(dirs, StubRunner(ok=False))
    client.post("/api/connect/start", json={"confirm_bind": True})
    snap = wait_idle(client)

    assert snap["state"] == "failed", snap
    assert snap["session_preserved"] is None
    assert not auth_path(dirs["cred"]).exists(), "不许留下可用的新登录态"
    assert not auth_path(dirs["cred"], "salt").exists(), "本次新建的 salt 也要撤回"


def test_t2c_late_thread_restores_real_auth_path(dirs):
    old_session = b"OLD-SESSION"
    auth_path(dirs["cred"]).parent.mkdir(parents=True, exist_ok=True)
    auth_path(dirs["cred"]).write_bytes(old_session)

    gate = threading.Event()
    runner = StubRunner(block=gate)
    client = make_client(dirs, runner)
    client.post("/api/connect/start", json={"confirm_bind": True})
    assert runner.started.wait(5.0)

    mgr = client.app.state.connect
    with mgr._lock:  # noqa: SLF001 - 制造"上游一直不返回"的时刻
        mgr._deadline = time.monotonic() - 1
    assert client.get("/api/connect/status").json()["state"] == "timeout"

    gate.set()
    snap = wait_idle(client)
    assert snap["state"] == "timeout", snap
    assert auth_path(dirs["cred"]).read_bytes() == old_session


# --------------------------------------------------------------------------- #
# T3 快照读失败 / 备份失败：login 零调用，且一个字节都不动
# --------------------------------------------------------------------------- #
def test_t3a_snapshot_read_failure_refuses_before_login(dirs):
    # 把真实凭据路径做成目录 → read_bytes() 抛 IsADirectoryError
    auth_path(dirs["cred"]).mkdir(parents=True, exist_ok=True)
    runner = StubRunner()
    client = make_client(dirs, runner)
    client.post("/api/connect/start", json={"confirm_bind": True})
    snap = wait_idle(client)

    assert runner.login_calls == 0, "快照失败必须在 login 之前拒绝"
    assert snap["state"] == "failed", snap
    assert snap["reason"].startswith("snapshot_failed:")
    assert auth_path(dirs["cred"]).is_dir(), "读失败不等于'原来不存在'，不许删原路径"


def test_t3b_backup_write_failure_refuses_before_login(dirs):
    old_session = b"OLD-SESSION"
    auth_path(dirs["cred"]).parent.mkdir(parents=True, exist_ok=True)
    auth_path(dirs["cred"]).write_bytes(old_session)
    # 让工作目录 `connect` 变成普通文件 → 备份落盘必然失败
    (dirs["root"] / "connect").write_text("not a dir", encoding="utf-8")

    runner = StubRunner(ok=False)
    client = make_client(dirs, runner)
    client.post("/api/connect/start", json={"confirm_bind": True})
    snap = wait_idle(client)

    assert runner.login_calls == 0, "备份失败必须拒绝在 login 之前"
    assert snap["state"] == "failed", snap
    assert snap["reason"].startswith("snapshot_failed:")
    assert auth_path(dirs["cred"]).read_bytes() == old_session, "原凭据不许被动过"


# =========================================================================== #
# 以下为 F2 / F3 / F4 的定向缺口（T4–T12）
# =========================================================================== #
def strict_manager(dirs, runner, **kw):
    """boss 模式形状的管理器：严格路径校验开启，凭据/预算目录都显式给。"""
    (dirs["budget"] / "config.json").write_text(
        json.dumps({"request_budget_dir": str(dirs["budget"])}), encoding="utf-8"
    )
    mgr = build_manager(
        {"adapter": "boss", "adapter_options": {"data_dir": str(dirs["cred"])}},
        dirs["root"] / "fix2.db",
        runner=runner,
    )
    for key, value in kw.items():
        setattr(mgr, key, value)
    return mgr


def test_t4_commit_failure_never_publishes_connected(dirs, monkeypatch):
    """绑定写入失败 → 不许出现 connected / matched，凭据回滚。"""
    old_session = b"OLD-SESSION"
    auth_path(dirs["cred"]).parent.mkdir(parents=True, exist_ok=True)
    auth_path(dirs["cred"]).write_bytes(old_session)

    runner = StubRunner(identity="identity-A")
    client = make_client(dirs, runner)
    mgr = client.app.state.connect

    def boom(digest):
        raise OSError("disk full")

    monkeypatch.setattr(mgr, "_write_binding", boom)
    client.post("/api/connect/start", json={"confirm_bind": True})
    snap = wait_idle(client)

    assert snap["state"] == "failed", snap
    assert snap["reason"].startswith("commit_failed:"), snap
    assert snap["binding"] == "not_applicable"
    assert auth_path(dirs["cred"]).read_bytes() == old_session
    assert not (dirs["root"] / "connect" / "account_binding.json").exists()


def test_t5_restore_failure_sets_recovery_todo_and_blocks(dirs, monkeypatch):
    """回滚失败/未知 → 落恢复待办：worker 退出后业务与新连接仍然拒绝。"""
    auth_path(dirs["cred"]).parent.mkdir(parents=True, exist_ok=True)
    auth_path(dirs["cred"]).write_bytes(b"OLD-SESSION")

    runner = StubRunner(ok=False)
    client = make_client(dirs, runner)
    mgr = client.app.state.connect

    monkeypatch.setattr(mgr, "_restore_credentials", lambda snapshot: False)
    client.post("/api/connect/start", json={"confirm_bind": True})
    snap = wait_idle(client)

    assert snap["state"] == "failed", snap
    assert (dirs["root"] / "connect" / "recovery_required.json").exists(), "必须留恢复待办"
    # worker 已退出，但业务与新连接都要继续拒绝
    assert client.post("/api/run/direct-greet").status_code == 409
    assert client.post("/api/connect/start", json={"confirm_bind": True}).status_code == 503
    assert client.get("/api/connect/status").json()["reason"] == "recovery_todo_present"


def test_t6_recovery_todo_survives_restart(dirs, monkeypatch):
    """恢复待办是持久标记：新进程（新 manager）也不许靠重启放行。"""
    auth_path(dirs["cred"]).parent.mkdir(parents=True, exist_ok=True)
    auth_path(dirs["cred"]).write_bytes(b"OLD-SESSION")
    first = make_client(dirs, StubRunner(ok=False))
    monkeypatch.setattr(first.app.state.connect, "_restore_credentials", lambda snapshot: False)
    first.post("/api/connect/start", json={"confirm_bind": True})
    wait_idle(first)

    fresh = make_client(dirs, StubRunner())
    assert fresh.get("/api/connect/status").json()["reason"] == "recovery_todo_present"
    assert fresh.post("/api/connect/start", json={"confirm_bind": True}).status_code == 503
    # 页面与只读 status 仍然可用（不因待办拖垮前端）
    assert fresh.get("/").status_code == 200
    assert fresh.get("/api/status").status_code == 200


def test_t7_corrupt_binding_refuses_even_with_confirm(dirs):
    """绑定 JSON 损坏 ≠ 未绑定：confirm_bind 不许覆盖，且不自动重建密钥再绑。"""
    work = dirs["root"] / "connect"
    work.mkdir(parents=True, exist_ok=True)
    (work / "account_binding.json").write_text("{not json", encoding="utf-8")
    (work / "secret.key").write_bytes(b"k" * 32)

    client = make_client(dirs, StubRunner(identity="identity-A"))
    client.post("/api/connect/start", json={"confirm_bind": True})
    snap = wait_idle(client)

    assert snap["state"] == "needs_user_action", snap
    assert snap["reason"] == "binding_corrupt"
    assert (work / "account_binding.json").read_text(encoding="utf-8") == "{not json"


def test_t8_atomic_admission_rejects_without_counting(dirs):
    """原子准入：拒绝时不得计数；准入后必须释放（异常也不泄漏、不负数）。"""
    mgr = make_manager(dirs, StubRunner())
    with mgr._lock:  # noqa: SLF001 - 直接制造"连接占用中"
        mgr._state = "connecting"
        mgr._worker_alive = True
    assert mgr.acquire_business() is not None
    assert mgr._business_inflight == 0, "拒绝时不许加计数"
    with mgr._lock:  # noqa: SLF001
        mgr._state = "disconnected"
        mgr._worker_alive = False
    assert mgr.acquire_business() is None
    assert mgr._business_inflight == 1
    mgr.release_business()
    mgr.release_business()  # 多释放一次不许出现负数
    assert mgr._business_inflight == 0


def test_t9_boss_mode_requires_explicit_absolute_cred_env(dirs, monkeypatch):
    budget = dirs["budget"]
    (budget / "config.json").write_text(json.dumps({"request_budget_dir": str(budget)}), encoding="utf-8")
    cfg = {"adapter": "boss", "adapter_options": {"data_dir": str(dirs["cred"])}}

    monkeypatch.delenv("JOBPILOT_BOSS_CRED_DIR", raising=False)
    monkeypatch.setenv("JOBPILOT_BOSS_BUDGET_DIR", str(budget))
    from app.api.connect import resolve_paths

    assert resolve_paths(cfg)[2] == "cred_env_missing"

    monkeypatch.setenv("JOBPILOT_BOSS_CRED_DIR", "relative/path")
    assert resolve_paths(cfg)[2] == "cred_env_relative"

    monkeypatch.setenv("JOBPILOT_BOSS_CRED_DIR", str(budget))  # 与适配器 data_dir 冲突
    assert resolve_paths(cfg)[2] == "cred_env_conflict_with_adapter"

    # 合法形态：凭据根的 config.json 把预算指到共享目录（上游规则），且与期望一致
    (dirs["cred"] / "config.json").write_text(
        json.dumps({"request_budget_dir": str(budget)}), encoding="utf-8"
    )
    monkeypatch.setenv("JOBPILOT_BOSS_CRED_DIR", str(dirs["cred"]))
    assert resolve_paths(cfg)[2] is None


def test_t10_budget_mismatch_and_drift_block(dirs, monkeypatch):
    budget = dirs["budget"]
    other = dirs["root"] / "other-budget"
    other.mkdir()
    (dirs["cred"] / "config.json").write_text(
        json.dumps({"request_budget_dir": str(other)}), encoding="utf-8"
    )
    cfg = {"adapter": "boss", "adapter_options": {"data_dir": str(dirs["cred"])}}
    monkeypatch.setenv("JOBPILOT_BOSS_CRED_DIR", str(dirs["cred"]))
    monkeypatch.setenv("JOBPILOT_BOSS_BUDGET_DIR", str(budget))
    from app.api.connect import resolve_paths

    assert resolve_paths(cfg)[2] == "budget_mismatch_with_upstream"

    # 运行中漂移：env 被改 → 拒绝（不支持热切换）
    (dirs["cred"] / "config.json").write_text(
        json.dumps({"request_budget_dir": str(budget)}), encoding="utf-8"
    )
    mgr = build_manager(cfg, dirs["root"] / "fix2.db", runner=StubRunner())
    monkeypatch.setenv("JOBPILOT_BOSS_BUDGET_DIR", str(other))
    assert "运行中被改变" in (mgr.blocked_reason_for_business() or "")


def test_t11_risk_lock_in_either_dir_blocks_both_entries(dirs):
    (dirs["budget"] / "cdp_risk_lock.json").write_text("{}", encoding="utf-8")
    mgr = make_manager(dirs, StubRunner())
    client = make_client(dirs, StubRunner())
    assert client.post("/api/connect/start", json={"confirm_bind": True}).status_code == 503
    assert client.post("/api/run/direct-greet").status_code == 200  # 业务侧由 dashboard 自己判锁
    assert mgr._risk_lock_file() is not None


def test_t12_identity_parsing_rejects_masked_and_role_fields(dirs):
    """S1：身份解析各分支 —— 掩码/角色/空值不许当唯一身份。"""
    from app.api.connect import UpstreamLoginRunner

    runner = UpstreamLoginRunner()

    def envelope(user):
        return {"ok": True, "data": {"user": user}}

    cases = [
        ({"phone": "13800000000"}, "v2:phone-cn:13800000000"),
        ({"phone": "138****0000", "identity": "求职者"}, None),   # 掩码
        ({"phone": "", "identity": "求职者"}, None),               # 只有角色字段
        ({"phone": "12345", "identity": "求职者"}, None),          # 残缺
        ({"name": "张三", "identity": "求职者"}, None),            # 只有姓名
    ]
    for user, expected in cases:
        runner._run = lambda argv, _e=envelope(user): _e  # type: ignore[assignment]
        got = runner.identity_source(cred_dir=dirs["cred"])
        assert got == expected, f"{user} → {got!r}，期望 {expected!r}"


def test_t8b_admission_race_stress(dirs):
    """T8b：并发压测"准入 vs 开始连接"，不允许出现"业务已持有 + 连接同时成立"。

    ⚠️ 性质说明（如实登记）：这是**概率性**压测，不是确定性证明。真正确定性证明靠
    实现上"检查 + 登记"在同一次持锁内完成（单点），压测只是补充。在把两次持锁拆开的
    注入副本（INJECT-I3b）上，本测试应当以一定概率打红；若某次没红，不代表实现正确。
    """
    manager = make_manager(dirs, StubRunner())
    violations: list[str] = []

    def business_worker(rounds: int):
        for _ in range(rounds):
            if manager.acquire_business() is None:
                try:
                    with manager._lock:  # noqa: SLF001 - 读内部占用状态做判定
                        if manager._state == "connecting" or manager._worker_alive:
                            violations.append("业务准入与连接占用同时成立")
                finally:
                    manager.release_business()

    def connect_worker(rounds: int):
        for _ in range(rounds):
            with manager._lock:  # noqa: SLF001 - 模拟 start() 的两道检查
                if manager._state != "connecting" and not manager._worker_alive \
                        and manager._business_inflight == 0:
                    manager._state = "connecting"
                    manager._worker_alive = True
            with manager._lock:  # noqa: SLF001 - 立刻收起，形成高频窗口
                manager._state = "disconnected"
                manager._worker_alive = False

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(business_worker, 300) for _ in range(3)]
        futures += [pool.submit(connect_worker, 300) for _ in range(3)]
        for fut in futures:
            fut.result(timeout=30)

    assert violations == [], f"出现 {len(violations)} 次准入竞争违规：{violations[:3]}"
    assert manager._business_inflight == 0
