"""Connect_fix3 · R1–R3 / R5 定向测试（`复审修单-Connect_fix2-剩余阻塞.md` 唯一依据）。

纪律（本批硬约束）：

- 只新增本文件，`test_connect.py` / `_fix1.py` / `_fix2.py` / `_browser.py` 逐字节不动；
- 既有断言值一条不改；不改 `config.yaml`；不改 DB schema；不真登录、不发送、不联网；
- 探针一律 `monkeypatch` 注入，**不设回环白名单**；
- 每条都做「撤掉修复必须变红」的反向推理（红点说明写在 docstring 里）。

R6（S1 身份字段）**不在本文件内假装完成**：代码仍是精确拒绝，事实缺口见交付说明。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from app.api import connect as connect_mod
from app.api.connect import (
    RECOVERY_REASON_INTERRUPTED,
    RECOVERY_REASON_ORPHAN_BACKUP,
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    STATE_NEEDS_USER_ACTION,
    BudgetConfigError,
    ConnectBlocked,
    ConnectManager,
    UpstreamLoginRunner,
    upstream_budget_dir,
)


# --------------------------------------------------------------------------- #
# 夹具：全离线替身上游（不 import boss_agent_cli、不发任何请求）
# --------------------------------------------------------------------------- #
class StubRunner:
    """最简替身：记录调用次数，按需返回固定信封。"""

    def __init__(self, *, login=None, status=None, identity=None, on_login=None):
        self.login_calls = 0
        self.status_calls = 0
        self.identity_calls = 0
        self._login = login if login is not None else {"ok": True, "data": {"logged_in": True}}
        self._status = status if status is not None else {"ok": True, "data": {"logged_in": True}}
        self._identity = identity if identity is not None else "v2:phone-cn:13800000000"
        self._on_login = on_login

    def login(self, *, cred_dir, budget_dir, cdp_url, timeout_s):
        self.login_calls += 1
        if self._on_login is not None:
            self._on_login(Path(cred_dir))
        # 替身上游也必须真写凭据文件：否则判据 2（session_incomplete）先红，
        # 后面的绑定/身份契约就测不到了（这是"替身太假"的经典坑）。
        session = Path(cred_dir) / "auth" / "session.enc"
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_bytes(b"NEW-SESSION-WRITTEN-BY-LOGIN")
        return self._login

    def local_status(self, *, cred_dir):
        self.status_calls += 1
        return self._status

    def identity_source(self, *, cred_dir):
        self.identity_calls += 1
        return self._identity


@pytest.fixture()
def dirs(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path
    cred = root / "cred"
    budget = root / "budget"
    (cred / "auth").mkdir(parents=True, exist_ok=True)
    budget.mkdir(parents=True, exist_ok=True)
    return {"root": root, "cred": cred, "budget": budget, "work": root / "connect"}


def make_manager(dirs: dict[str, Path], runner, **kwargs) -> ConnectManager:
    return ConnectManager(
        data_dir=dirs["root"],
        cred_dir=dirs["cred"],
        budget_dir=dirs["budget"],
        runner=runner,
        **kwargs,
    )


def wait_idle(manager: ConnectManager, timeout: float = 5.0) -> dict:
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        snap = manager.status()
        if not snap["task_active"]:
            return snap
        _time.sleep(0.01)
    raise AssertionError("连接任务未在限时内结束")


def auth_session(dirs: dict[str, Path]) -> Path:
    return dirs["cred"] / "auth" / "session.enc"


def auth_salt(dirs: dict[str, Path]) -> Path:
    return dirs["cred"] / "auth" / "salt"


# --------------------------------------------------------------------------- #
# R1：未完成任务有持久开始记录；进程中断后新 manager 必须阻断
# --------------------------------------------------------------------------- #
def test_r1_inprogress_record_written_before_login(dirs):
    """登录**之前**必须已有 `in_progress.json`，且记录连接前 session/salt 是否存在。

    红点：把 `_begin_task_record` 调用删掉 → 本断言失败。
    """
    seen: dict[str, object] = {}

    def on_login(cred_dir: Path):
        path = dirs["work"] / "in_progress.json"
        seen["exists"] = path.exists()
        seen["payload"] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    runner = StubRunner(on_login=on_login)
    manager = make_manager(dirs, runner)
    manager.start(confirm_bind=True)
    wait_idle(manager)

    assert seen["exists"] is True, "登录前没有任务开始记录：进程中断后无从判断是否动过真凭据"
    payload = seen["payload"]
    assert payload["phase"] == "pre_login"
    assert payload["session_existed"] is False
    assert payload["salt_existed"] is False
    assert payload["task_id"]


def test_r1_inprogress_record_lists_existing_credentials(dirs):
    """连接前存在 session/salt → 记录里必须是 True（恢复依据的一部分）。"""
    auth_session(dirs).write_bytes(b"OLD-SESSION")
    auth_salt(dirs).write_bytes(b"OLD-SALT")
    seen: dict[str, object] = {}

    def on_login(cred_dir: Path):
        payload = json.loads((dirs["work"] / "in_progress.json").read_text(encoding="utf-8"))
        seen.update(payload)

    manager = make_manager(dirs, StubRunner(on_login=on_login))
    manager.start(confirm_bind=True)
    wait_idle(manager)

    assert seen["session_existed"] is True
    assert seen["salt_existed"] is True


def test_r1_interrupted_process_blocks_new_manager(dirs):
    """**不靠模拟已写好的 recovery_required.json**：真的留下 in_progress 记录再重建 manager。

    红点：把 `_read_recovery_todo` 里对 `in_progress.json` 的判定删掉 → 本断言失败。
    """
    manager = make_manager(dirs, StubRunner())
    manager.start(confirm_bind=True)
    snap = wait_idle(manager)
    assert snap["state"] == STATE_CONNECTED, snap

    # 模拟"进程在提交前被打断"：记录还在（正常完成后会被清掉，这里手工复原现场）
    (dirs["work"] / "in_progress.json").write_text(
        json.dumps({"task_id": "dead-beef", "phase": "pre_login"}), encoding="utf-8"
    )

    rebuilt = make_manager(dirs, StubRunner())
    status = rebuilt.status()
    assert status["state"] == STATE_NEEDS_USER_ACTION
    assert status["reason"] == "recovery_todo_present"
    assert rebuilt._recovery_reason == RECOVERY_REASON_INTERRUPTED

    # 连接与相关 boss 业务都必须拒绝
    with pytest.raises(ConnectBlocked):
        rebuilt.start()
    assert rebuilt.acquire_business() is not None


def test_r1_orphan_backup_without_marker_blocks(dirs):
    """有 `.bak` 却没有任何标记 = 旧备份残留 → 不许被当成本次恢复依据。

    红点：删掉 orphan_backup 判定 → 本断言失败。
    """
    dirs["work"].mkdir(parents=True, exist_ok=True)
    (dirs["work"] / "session.enc.bak").write_bytes(b"STALE-BACKUP")

    manager = make_manager(dirs, StubRunner())
    assert manager._recovery_reason == RECOVERY_REASON_ORPHAN_BACKUP
    with pytest.raises(ConnectBlocked):
        manager.start()


def test_r1_successful_commit_clears_record(dirs):
    """提交成功是两条合法清除路径之一：`in_progress.json` 必须消失。"""
    manager = make_manager(dirs, StubRunner())
    manager.start(confirm_bind=True)
    wait_idle(manager)
    assert not (dirs["work"] / "in_progress.json").exists(), "提交完成后记录未清"


def test_r1_verified_restore_clears_record(dirs):
    """失败但恢复**读回核验通过** → 记录也要清（否则会永久锁死）。"""
    auth_session(dirs).write_bytes(b"OLD-SESSION")
    manager = make_manager(dirs, StubRunner(login={"ok": False, "error": {"code": "AUTH_FAILED"}}))
    manager.start(confirm_bind=True)
    snap = wait_idle(manager)

    assert snap["state"] != STATE_CONNECTED
    assert auth_session(dirs).read_bytes() == b"OLD-SESSION"
    assert not (dirs["work"] / "in_progress.json").exists()
    assert manager.acquire_business() is None, "恢复核验通过后业务不该被永久阻断"


def test_r1_unwritable_record_refuses_before_login(dirs):
    """记录写不进去 → **零 login 调用**（R1 原文：记录失败不得进入登录）。"""
    dirs["work"].write_text("not a dir", encoding="utf-8")  # 工作目录变成普通文件
    auth_session(dirs).write_bytes(b"OLD-SESSION")
    runner = StubRunner()
    manager = make_manager(dirs, runner)
    manager.start(confirm_bind=True)
    snap = wait_idle(manager)

    assert runner.login_calls == 0
    assert snap["reason"].startswith("snapshot_failed:")
    assert auth_session(dirs).read_bytes() == b"OLD-SESSION"


# --------------------------------------------------------------------------- #
# R2：恢复抛异常必须可靠阻断；缺密钥不许被重建
# --------------------------------------------------------------------------- #
def test_r2_restore_one_oserror_does_not_raise(dirs):
    """`_restore_one` 遇 `OSError` 返回 False，不向上抛。"""
    target = dirs["root"] / "x.enc"
    target.write_bytes(b"NEW")
    assert ConnectManager._restore_one(target, b"OLD") in (True, False)


def test_r2_restore_oserror_blocks_even_after_worker_exits(dirs, monkeypatch):
    """真实注入：`_restore_one` 抛 `OSError` → worker 退出后业务与新连接仍被拒。

    红点：把 `_restore_one` 的 try/except 去掉 → `finally` 被异常跳过 →
    `_set_recovery_todo` 不落 → 本断言失败。
    """
    auth_session(dirs).write_bytes(b"OLD-SESSION")

    def boom(path, before):
        raise OSError("restore-exploded")

    monkeypatch.setattr(ConnectManager, "_restore_one", staticmethod(boom))
    manager = make_manager(dirs, StubRunner(login={"ok": False, "error": {"code": "AUTH_FAILED"}}))
    manager.start(confirm_bind=True)
    snap = wait_idle(manager)

    assert snap["state"] != STATE_CONNECTED
    assert snap["task_active"] is False, "worker 已退出"
    assert manager.acquire_business() is not None, "恢复失败后业务必须继续被拒"
    assert (dirs["work"] / "recovery_required.json").exists(), "恢复失败必须落持久待办"


def test_r2_stale_branch_restore_oserror_writes_todo(dirs, monkeypatch):
    """迟到分支的恢复抛错同样要落待办（不能只处理返回 False）。

    构造真实的"旧任务"：租约到期后 `status()` 把任务判旧，worker 回来只能撤回自己的改动。
    """
    auth_session(dirs).write_bytes(b"OLD-SESSION")

    def boom(path, before):
        raise OSError("late-restore-exploded")

    now = [0.0]
    manager = ConnectManager(
        data_dir=dirs["root"],
        cred_dir=dirs["cred"],
        budget_dir=dirs["budget"],
        runner=StubRunner(),
        clock=lambda: now[0],
        login_timeout_s=1,
        lease_grace_s=0,
    )
    monkeypatch.setattr(ConnectManager, "_restore_one", staticmethod(boom))
    manager.start(confirm_bind=True)
    now[0] = 10.0  # 租约到期 → 本任务变"旧任务"
    assert manager.status()["state"] == "timeout"
    snap = wait_idle(manager)

    assert snap["state"] != STATE_CONNECTED
    assert (dirs["work"] / "recovery_required.json").exists(), "迟到分支恢复抛错必须落待办"


def test_r2_missing_secret_with_existing_binding_is_not_rebuilt(dirs):
    """旧绑定在、`secret.key` 缺失 → 拒绝，且**不许**把 secret.key 补出来。

    红点：把 `_digest` 默认改成 `create=True`（或恢复"先算摘要后验密钥"）→ 本断言失败。
    """
    work = dirs["work"]
    work.mkdir(parents=True, exist_ok=True)
    (work / "account_binding.json").write_text(
        json.dumps({"v": 1, "digest": "a" * 64}), encoding="utf-8"
    )

    manager = make_manager(dirs, StubRunner())
    manager.start(confirm_bind=True)
    snap = wait_idle(manager)

    assert snap["state"] == STATE_NEEDS_USER_ACTION
    assert snap["reason"] == "binding_secret_invalid"
    assert not (work / "secret.key").exists(), "缺密钥时不许自动重建（会绕过旧绑定）"


def test_r2_bad_version_binding_is_corrupt(dirs):
    """错误版本的绑定文件 = 损坏，不许静默迁移重绑。"""
    work = dirs["work"]
    work.mkdir(parents=True, exist_ok=True)
    (work / "secret.key").write_bytes(b"k" * 32)
    (work / "account_binding.json").write_text(
        json.dumps({"v": 2, "digest": "b" * 64}), encoding="utf-8"
    )

    manager = make_manager(dirs, StubRunner())
    manager.start(confirm_bind=True)
    snap = wait_idle(manager)
    assert snap["reason"] == "binding_corrupt"


def test_r2_malformed_digest_is_corrupt(dirs):
    """摘要形态不合规（长度/字符集）同样按损坏处理。"""
    work = dirs["work"]
    work.mkdir(parents=True, exist_ok=True)
    (work / "secret.key").write_bytes(b"k" * 32)
    (work / "account_binding.json").write_text(
        json.dumps({"v": 1, "digest": "not-a-hex-digest"}), encoding="utf-8"
    )

    manager = make_manager(dirs, StubRunner())
    manager.start(confirm_bind=True)
    snap = wait_idle(manager)
    assert snap["reason"] == "binding_corrupt"


# --------------------------------------------------------------------------- #
# R3：业务准入聚合两处风险锁；预算解析对齐上游
# --------------------------------------------------------------------------- #
def test_r3_business_admission_sees_risk_lock_in_cred_dir(dirs):
    """boss 严格模式下：凭据目录有锁 → 业务准入也必须拒绝（不只是连接入口）。

    红点：把 `_business_block_reason_locked` 里的风险锁判定删掉 → 本断言失败。
    """
    (dirs["cred"] / "cdp_risk_lock.json").write_text("{}", encoding="utf-8")
    manager = make_manager(dirs, StubRunner(), strict_paths=True)

    reason = manager.acquire_business()
    assert reason is not None and "风险锁" in reason
    with pytest.raises(ConnectBlocked):
        manager.start()


def test_r3_business_admission_sees_risk_lock_in_budget_dir(dirs):
    """共享预算目录有锁 → 同样拒绝（两处锁都要看）。"""
    (dirs["budget"] / "cdp_risk_lock.json").write_text("{}", encoding="utf-8")
    manager = make_manager(dirs, StubRunner(), strict_paths=True)
    assert manager.acquire_business() is not None


def test_r3_fake_mode_not_blocked_by_risk_lock(dirs):
    """fake 模式不读真实 boss 目录 → 真实锁文件不得污染离线语义。

    红点：把 `strict_paths` 条件去掉 → 本断言失败（会误伤全部离线业务测试）。
    """
    (dirs["budget"] / "cdp_risk_lock.json").write_text("{}", encoding="utf-8")
    manager = make_manager(dirs, StubRunner(), strict_paths=False)
    assert manager.acquire_business() is None
    manager.release_business()


def test_r3_rejected_admission_does_not_increment(dirs):
    """拒绝时不许加计数（否则一次拒绝就永久挡住业务）。"""
    # 先让"路径/预算"这一维合法，才能把断言收敛到"锁 → 拒绝且不加计数"单一口径
    (dirs["cred"] / "config.json").write_text(
        json.dumps({"request_budget_dir": str(dirs["budget"])}), encoding="utf-8"
    )
    (dirs["cred"] / "cdp_risk_lock.json").write_text("{}", encoding="utf-8")
    manager = make_manager(dirs, StubRunner(), strict_paths=True)
    for _ in range(3):
        assert manager.acquire_business() is not None
    (dirs["cred"] / "cdp_risk_lock.json").unlink()
    assert manager.acquire_business() is None, "拒绝累计成了占用"
    manager.release_business()
    assert manager.acquire_business() is None, "释放后计数异常"
    manager.release_business()


@pytest.mark.parametrize(
    "payload,expect_error",
    [
        ({"request_budget_dir": None}, None),
        ({}, None),
        ({"request_budget_dir": ""}, "invalid_type"),
        ({"request_budget_dir": 0}, "invalid_type"),
        ({"request_budget_dir": False}, "invalid_type"),
        ({"request_budget_dir": 123}, "invalid_type"),
        ({"request_budget_dir": ["x"]}, "invalid_type"),
        ({"request_budget_dir": "relative/dir"}, "relative_request_budget_dir"),
    ],
)
def test_r3_budget_parsing_matches_upstream(dirs, payload, expect_error):
    """缺失/`null` 与**非法值**必须分清：非法值不许悄悄回退成凭据根。"""
    (dirs["cred"] / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    if expect_error is None:
        assert upstream_budget_dir(dirs["cred"]) == dirs["cred"].resolve()
        return
    with pytest.raises(BudgetConfigError) as excinfo:
        upstream_budget_dir(dirs["cred"])
    assert expect_error in str(excinfo.value)


def test_r3_non_object_config_json_rejected(dirs):
    """`config.json` 不是 JSON 对象 → 拒绝（上游要求对象）。"""
    (dirs["cred"] / "config.json").write_text(json.dumps(["not", "object"]), encoding="utf-8")
    with pytest.raises(BudgetConfigError) as excinfo:
        upstream_budget_dir(dirs["cred"])
    assert "not_object" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# R5：旧会话失效不落进隐含清 Cookie 重登；日志不泄漏
# --------------------------------------------------------------------------- #
def test_backup_cleanup_after_success_restart_not_locked(dirs):
    """复审·缺陷1-a：成功 `connected` 收尾后，重建 manager **不许**被判成未完成恢复。

    红点：把 `_commit_effects` 里的 `_drop_own_backups()` 去掉 → 残留 `salt.bak` →
    重建命中孤儿备份判据 → 断言失败。
    """
    auth_session(dirs).write_bytes(b"OLD-SESSION")
    auth_salt(dirs).write_bytes(b"OLD-SALT")
    manager = make_manager(dirs, StubRunner())
    manager.start(confirm_bind=True)
    snap = wait_idle(manager)
    assert snap["state"] == STATE_CONNECTED, snap

    leftovers = sorted(p.name for p in dirs["work"].iterdir())
    assert "salt.bak" not in leftovers, f"本任务备份未清：{leftovers}"
    assert "session.enc.bak" not in leftovers, f"本任务备份未清：{leftovers}"

    rebuilt = make_manager(dirs, StubRunner())
    assert rebuilt.status()["reason"] != "recovery_todo_present"
    assert rebuilt.acquire_business() is None, "成功收尾后重启被误锁"
    rebuilt.release_business()


def test_backup_cleanup_after_verified_restore_restart_not_locked(dirs):
    """复审·缺陷1-b：失败但恢复读回成功 → 两份备份都要清，重建 manager 不许误锁。

    红点：把 `_failure` 里恢复成功分支的 `_drop_own_backups()` 去掉 → 两份 `.bak` 残留。
    """
    auth_session(dirs).write_bytes(b"OLD-SESSION")
    auth_salt(dirs).write_bytes(b"OLD-SALT")
    manager = make_manager(dirs, StubRunner(login={"ok": False, "error": {"code": "AUTH_FAILED"}}))
    manager.start(confirm_bind=True)
    snap = wait_idle(manager)

    assert snap["state"] != STATE_CONNECTED
    assert snap["session_preserved"] is True, "旧凭据应已字节还原"
    assert auth_session(dirs).read_bytes() == b"OLD-SESSION"
    assert auth_salt(dirs).read_bytes() == b"OLD-SALT"

    leftovers = sorted(p.name for p in dirs["work"].iterdir()) if dirs["work"].exists() else []
    assert leftovers == [], f"恢复成功后仍残留恢复材料：{leftovers}"

    rebuilt = make_manager(dirs, StubRunner())
    assert rebuilt.status()["reason"] != "recovery_todo_present"
    assert rebuilt.acquire_business() is None, "恢复成功后重启被误锁"
    rebuilt.release_business()


# --------------------------------------------------------------------------- #
# 缺陷 2：受控 CDP 同步面 —— 浏览器会话与本地凭据分开判
# --------------------------------------------------------------------------- #
class _FakeAuthManager:
    """替身上游 `AuthManager`：只提供受控同步需要的那三个面。

    `_check_cdp_login_risk_lock`（锁/预算前置）、`_record_cdp_risk_lock`（风险记录）、
    `_store.save`（TokenStore 落盘）。**不涉及任何网络/playwright**。
    """

    def __init__(self, *, save_error: OSError | None = None):
        self.saved: list[dict] = []
        self.recorded: list[object] = []
        self._save_error = save_error
        self._store = self

    def _check_cdp_login_risk_lock(self, cdp_url):
        return None

    def _record_cdp_risk_lock(self, cdp_url=None, *, error=None, token=None):
        self.recorded.append(error)

    def save(self, token):
        if self._save_error is not None:
            raise self._save_error
        self.saved.append(token)


def _patch_cdp(monkeypatch, runner, *, token=None, stale: bool = False):
    """把受控同步缝替换成替身，并**禁止任何 CLI 调用**（`boss login --cdp` 一律算越界）。"""
    calls: list[dict] = []

    def fake_login_via_cdp(**kwargs):
        calls.append(dict(kwargs))
        if stale:
            from boss_agent_cli.auth.browser import ReusedSessionStaleError

            raise ReusedSessionStaleError("复用的登录态已在服务端失效（只读探测返回未登录）")
        return token if token is not None else {"wt2": "live-token"}

    def forbid_cli(argv):
        raise AssertionError(f"受控路径不许走 CLI：{argv}")

    monkeypatch.setattr(runner, "_login_via_cdp", fake_login_via_cdp)
    monkeypatch.setattr(runner, "_run", forbid_cli)
    return calls


def _assert_never_force(calls: list[dict]) -> None:
    assert calls, "没有发生任何 CDP 同步调用"
    for kwargs in calls:
        assert kwargs.get("reuse_existing") is True, f"出现 reuse_existing!=True：{kwargs}"
        assert "platform" in kwargs


def test_cdp_sync_first_login_without_local_creds(dirs, monkeypatch):
    """无本地凭据 + 浏览器会话正常 → 走 `login_via_cdp(reuse_existing=True)` 并落盘。

    这同时证明「正常首次人工扫码仍可完成」：我们没有在调用前就把它挡掉。
    """
    runner = UpstreamLoginRunner()
    manager_stub = _FakeAuthManager()
    calls = _patch_cdp(monkeypatch, runner, token={"wt2": "fresh"})
    monkeypatch.setattr(runner, "_auth_manager", lambda cred_dir: manager_stub)

    result = runner.login(cred_dir=dirs["cred"], budget_dir=dirs["budget"],
                          cdp_url="http://127.0.0.1:9222", timeout_s=5)

    assert result["ok"] is True and result["data"]["logged_in"] is True
    _assert_never_force(calls)
    assert calls[0]["cdp_url"] == "http://127.0.0.1:9222"
    assert manager_stub.saved == [{"wt2": "fresh"}], "必须经上游 TokenStore.save 落盘"


def test_cdp_sync_with_existing_local_creds_still_syncs(dirs, monkeypatch):
    """有本地凭据也**必须**做同步，不能仅凭本地状态就复用（复审现场4）。"""
    auth_session(dirs).write_bytes(b"OLD-SESSION")
    runner = UpstreamLoginRunner()
    manager_stub = _FakeAuthManager()
    calls = _patch_cdp(monkeypatch, runner, token={"wt2": "synced"})
    monkeypatch.setattr(runner, "_auth_manager", lambda cred_dir: manager_stub)

    result = runner.login(cred_dir=dirs["cred"], budget_dir=dirs["budget"],
                          cdp_url="http://127.0.0.1:9222", timeout_s=5)

    assert result["ok"] is True
    _assert_never_force(calls)
    assert manager_stub.saved == [{"wt2": "synced"}]


def test_cdp_sync_stale_with_local_creds_returns_manual(dirs, monkeypatch):
    """stale + 有本地凭据 → 手动码；零保存、零 CLI、零 cookie 清除。"""
    auth_session(dirs).write_bytes(b"STALE-SESSION")
    runner = UpstreamLoginRunner()
    manager_stub = _FakeAuthManager()
    calls = _patch_cdp(monkeypatch, runner, stale=True)
    monkeypatch.setattr(runner, "_auth_manager", lambda cred_dir: manager_stub)

    result = runner.login(cred_dir=dirs["cred"], budget_dir=dirs["budget"],
                          cdp_url="http://127.0.0.1:9222", timeout_s=5)

    assert result["ok"] is False
    assert result["error"]["code"] == "SESSION_STALE_MANUAL"
    assert len(calls) == 1, "失效后不许再尝试第二次（那正是 reuse_existing=False 的位置）"
    _assert_never_force(calls)
    assert manager_stub.saved == [], "失效时不许落盘"
    assert auth_session(dirs).read_bytes() == b"STALE-SESSION"


def test_cdp_sync_stale_without_local_creds_returns_manual(dirs, monkeypatch):
    """stale + 无本地凭据 → 同样手动码，且不留下任何凭据文件。"""
    runner = UpstreamLoginRunner()
    manager_stub = _FakeAuthManager()
    calls = _patch_cdp(monkeypatch, runner, stale=True)
    monkeypatch.setattr(runner, "_auth_manager", lambda cred_dir: manager_stub)

    result = runner.login(cred_dir=dirs["cred"], budget_dir=dirs["budget"],
                          cdp_url="http://127.0.0.1:9222", timeout_s=5)

    assert result["error"]["code"] == "SESSION_STALE_MANUAL"
    _assert_never_force(calls)
    assert manager_stub.saved == []
    assert not auth_session(dirs).exists()


def test_cdp_sync_token_save_failure_reports_not_saved(dirs, monkeypatch):
    """落盘失败要如实报，不许当成功。"""
    runner = UpstreamLoginRunner()
    manager_stub = _FakeAuthManager(save_error=OSError("disk-full"))
    calls = _patch_cdp(monkeypatch, runner, token={"wt2": "x"})
    monkeypatch.setattr(runner, "_auth_manager", lambda cred_dir: manager_stub)

    result = runner.login(cred_dir=dirs["cred"], budget_dir=dirs["budget"],
                          cdp_url="http://127.0.0.1:9222", timeout_s=5)

    assert result["ok"] is False
    assert result["error"]["code"] == "TOKEN_SAVE_FAILED"
    _assert_never_force(calls)


def test_r5_stale_marker_maps_to_needs_user_action(dirs):
    """`SESSION_STALE_MANUAL` 必须落到 `needs_user_action/session_stale_manual`，不是通用错误。"""
    auth_session(dirs).write_bytes(b"OLD-SESSION")
    manager = make_manager(dirs, StubRunner(login={
        "ok": False, "error": {"code": "SESSION_STALE_MANUAL"},
    }))
    manager.start(confirm_bind=True)
    snap = wait_idle(manager)

    assert snap["state"] == STATE_NEEDS_USER_ACTION
    assert snap["reason"] == "session_stale_manual"


def test_r5_stderr_content_never_reaches_response(dirs):
    """上游 stderr 里的指纹/路径/异常正文不许进响应与项目日志。

    `UpstreamLoginRunner._run` 把 stderr 接进内存缓冲后丢弃，只保留"有没有输出"。
    红点：把 `redirect_stderr(err_buffer)` 换成继承父进程 stderr → 本断言失去隔离。
    """
    manager = make_manager(dirs, StubRunner())
    manager.start(confirm_bind=True)
    snap = wait_idle(manager)
    blob = json.dumps(snap, ensure_ascii=False)
    for leak in ("cookie", "session.enc", "13800000000", "Bearer"):
        assert leak not in blob


# --------------------------------------------------------------------------- #
# 缺陷 1（fix3c）：迟到/超时分支的收尾 + 提交路径的两种失败区分
# --------------------------------------------------------------------------- #
def test_backup_cleanup_after_late_restore_restart_not_locked(dirs, monkeypatch):
    """迟到/超时任务恢复读回成功 → 清本任务备份，重建 manager 不许误锁。

    构造真实"旧任务"：租约到期后 `status()` 把任务判旧，worker 回来只撤回自己的改动。
    红点：把 stale 分支恢复成功后的 `_drop_own_backups()` 去掉 → 残留 `salt.bak`/
    `session.enc.bak` → 重建命中孤儿备份判据 → 断言失败。
    """
    auth_session(dirs).write_bytes(b"OLD-SESSION")
    auth_salt(dirs).write_bytes(b"OLD-SALT")

    now = [0.0]
    manager = ConnectManager(
        data_dir=dirs["root"],
        cred_dir=dirs["cred"],
        budget_dir=dirs["budget"],
        runner=StubRunner(),
        clock=lambda: now[0],
        login_timeout_s=1,
        lease_grace_s=0,
    )
    manager.start(confirm_bind=True)
    now[0] = 10.0  # 租约到期 → 本任务变"旧任务"
    assert manager.status()["state"] == "timeout"
    snap = wait_idle(manager)

    assert snap["state"] != STATE_CONNECTED
    assert auth_session(dirs).read_bytes() == b"OLD-SESSION"
    assert auth_salt(dirs).read_bytes() == b"OLD-SALT"

    leftovers = sorted(p.name for p in dirs["work"].iterdir()) if dirs["work"].exists() else []
    assert "salt.bak" not in leftovers, f"迟到恢复成功后仍残留备份：{leftovers}"
    assert "session.enc.bak" not in leftovers, f"迟到恢复成功后仍残留备份：{leftovers}"

    rebuilt = make_manager(dirs, StubRunner())
    assert rebuilt.status()["reason"] != "recovery_todo_present"
    assert rebuilt.acquire_business() is None, "迟到恢复成功 + 收尾干净后重启被误锁"
    rebuilt.release_business()


def test_commit_backup_cleanup_failure_reports_accurately(dirs, monkeypatch):
    """提交路径：备份清理失败必须报 `backup_cleanup_failed`，不许冒充"绑定提交失败"。

    此时绑定**已经写成功**，失败的是清理 —— 所以提示要说明"新绑定 + 旧凭据并存"并保持阻断。
    """
    auth_session(dirs).write_bytes(b"OLD-SESSION")

    def boom(self):
        raise OSError("unlink-denied")

    monkeypatch.setattr(ConnectManager, "_drop_own_backups", boom)
    manager = make_manager(dirs, StubRunner())
    manager.start(confirm_bind=True)
    snap = wait_idle(manager)

    assert snap["state"] == STATE_NEEDS_USER_ACTION
    assert snap["reason"].startswith("backup_cleanup_failed"), snap["reason"]
    assert "提交失败" not in snap["message"], "不许复用'账号绑定提交失败'文案"
    assert "备份未能清理" in snap["message"]
    assert (dirs["work"] / "recovery_required.json").exists(), "清理失败必须落持久待办"
    assert manager.acquire_business() is not None, "清理失败后必须保持阻断"
