"""Connect_fix3 · R4 定向测试：真实探测解析 + 生产默认装配路径。

覆盖复审 R4 的三处缺口：

1. `listener_pids/process_exe/process_cmdline` 不再是 `NotImplementedError`（生产可用）；
2. 归属核验补**实际监听地址**与 **PID 创建时间**（防 PID 复用/非回环）；
3. 启动前核 profile 占用、启动后核进程是否提前退出；`main.py` 真实装配。

纪律：不打开真实 Chrome、不联网、不依赖 `psutil`。真机 CDP/进程归属仍待 Windows 验收。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.connect_browser import (
    CDP_PORT,
    BrowserConfig,
    BrowserLaunchError,
    BrowserOwnershipError,
    ConnectBrowser,
    ListenerInfo,
    PortProbe,
    UnavailableBrowser,
    _parse_netstat,
    _split_cmdline,
)


# --------------------------------------------------------------------------- #
# 解析层（纯函数，离线可测；不碰真机）
# --------------------------------------------------------------------------- #
def test_parse_netstat_extracts_awake_listener_with_actual_address():
    """只认 LISTENING 行，并且要能拿到**实际监听地址**（不是命令行参数）。"""
    text = (
        "  Proto  Local Address          Foreign Address        State           PID\r\n"
        "  TCP    127.0.0.1:9222         0.0.0.0:0              LISTENING       4321\r\n"
        "  TCP    127.0.0.1:9222         127.0.0.1:51234        ESTABLISHED     4321\r\n"
        "  TCP    0.0.0.0:8010           0.0.0.0:0              LISTENING       1111\r\n"
    )
    infos = _parse_netstat(text, CDP_PORT)
    assert infos == [ListenerInfo(pid=4321, addr="127.0.0.1", port=CDP_PORT)]


def test_parse_netstat_handles_ipv6_and_ignores_other_ports():
    text = (
        "  TCP    [::1]:9222             [::]:0                 LISTENING       777\r\n"
        "  TCP    127.0.0.1:9333         0.0.0.0:0              LISTENING       888\r\n"
    )
    infos = _parse_netstat(text, CDP_PORT)
    assert [(i.pid, i.addr) for i in infos] == [(777, "::1")]


def test_split_cmdline_keeps_quoted_paths_with_spaces():
    """Windows 路径带空格/中文时不许被空白拆碎。"""
    parts = _split_cmdline(
        '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
        '"--user-data-dir=D:\\专用 目录\\profile" --remote-debugging-port=9222'
    )
    assert parts[0].endswith("chrome.exe")
    assert parts[1] == "--user-data-dir=D:\\专用 目录\\profile"
    assert parts[2] == "--remote-debugging-port=9222"


def test_default_port_probe_is_not_notimplemented():
    """R4 原始缺陷：默认 `PortProbe` 三个方法全是 `NotImplementedError` → 生产跑不起来。"""
    probe = PortProbe()
    assert isinstance(probe.listener_pids(CDP_PORT), set)
    assert probe.process_exe(0) is None or isinstance(probe.process_exe(0), str)
    assert probe.process_cmdline(0) is None or isinstance(probe.process_cmdline(0), list)
    # 非 Windows 一律 fail-closed（None），绝不猜
    assert probe.process_create_time(0) is None or isinstance(probe.process_create_time(0), str)


# --------------------------------------------------------------------------- #
# 归属核验（桩探针覆盖全部维度）
# --------------------------------------------------------------------------- #
class FullProbe:
    """带全维度的桩：监听地址 / exe / 命令行 / 创建时间 / profile 占用 / CDP。"""

    def __init__(self, *, listeners=(), exe=None, cmdline=None,
                 create_time="2026-10-30 10:00:00", cdp=True, owners=()):
        self._listeners = list(listeners)
        self._exe = exe
        self._cmdline = cmdline
        self._create_time = create_time
        self._cdp = cdp
        self._owners = set(owners)

    def listeners(self, port):
        return [ListenerInfo(pid=pid, addr=addr, port=port) for pid, addr in self._listeners]

    def listener_pids(self, port):
        return {i.pid for i in self.listeners(port)}

    def listener_addresses(self, port):
        return {i.addr for i in self.listeners(port)}

    def process_exe(self, pid):
        return self._exe

    def process_cmdline(self, pid):
        return self._cmdline

    def process_create_time(self, pid):
        return self._create_time() if callable(self._create_time) else self._create_time

    def processes_with_profile(self, norm):
        return set(self._owners)

    def cdp_version(self, url, timeout_s=2.0):
        return {"Browser": "Chrome/140"} if self._cdp else None


@pytest.fixture()
def cfg(tmp_path: Path) -> BrowserConfig:
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"MZ")
    profile = tmp_path / "profile"
    profile.mkdir()
    return BrowserConfig(exe=exe, profile_dir=profile)


def _cmdline(cfg: BrowserConfig) -> list[str]:
    return [
        str(cfg.exe),
        f"--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={cfg.profile_dir}",
    ]


def test_ownership_accepts_matching_loopback_listener(cfg):
    probe = FullProbe(listeners=[(4321, "127.0.0.1")], exe=str(cfg.exe),
                      cmdline=_cmdline(cfg))
    handle = ConnectBrowser(cfg, probe=probe).verify_ownership()
    assert handle is not None and handle.launched is False
    assert handle.pid == 4321
    assert handle.started_at == "2026-10-30 10:00:00", "创建时间必须参与归属证据"


def test_ownership_rejects_non_loopback_actual_listener(cfg):
    """命令行写着 127.0.0.1 但**实际监听** 0.0.0.0 → 拒绝（只核参数等于没核）。"""
    probe = FullProbe(listeners=[(4321, "0.0.0.0")], exe=str(cfg.exe), cmdline=_cmdline(cfg))
    with pytest.raises(BrowserOwnershipError):
        ConnectBrowser(cfg, probe=probe).verify_ownership()


def test_ownership_rejects_when_create_time_unreadable(cfg):
    """读不到创建时间 = 无法排除 PID 复用 → fail-closed。"""
    probe = FullProbe(listeners=[(4321, "127.0.0.1")], exe=str(cfg.exe),
                      cmdline=_cmdline(cfg), create_time=None)
    with pytest.raises(BrowserOwnershipError):
        ConnectBrowser(cfg, probe=probe).verify_ownership()


def test_ownership_rejects_two_listeners(cfg):
    probe = FullProbe(listeners=[(4321, "127.0.0.1"), (4322, "127.0.0.1")],
                      exe=str(cfg.exe), cmdline=_cmdline(cfg))
    with pytest.raises(BrowserOwnershipError):
        ConnectBrowser(cfg, probe=probe).verify_ownership()


def test_ownership_rejects_wrong_exe_and_wrong_profile(cfg, tmp_path):
    other = tmp_path / "other.exe"
    other.write_bytes(b"MZ")
    probe = FullProbe(listeners=[(4321, "127.0.0.1")], exe=str(other), cmdline=_cmdline(cfg))
    with pytest.raises(BrowserOwnershipError):
        ConnectBrowser(cfg, probe=probe).verify_ownership()

    probe2 = FullProbe(listeners=[(4321, "127.0.0.1")], exe=str(cfg.exe),
                       cmdline=[str(cfg.exe), "--user-data-dir=" + str(tmp_path / "daily")])
    with pytest.raises(BrowserOwnershipError):
        ConnectBrowser(cfg, probe=probe2).verify_ownership()


# --------------------------------------------------------------------------- #
# 启动 / 复用
# --------------------------------------------------------------------------- #
def test_ensure_refuses_when_profile_owned_by_other_process(cfg):
    """端口空闲 ≠ profile 空闲：别的进程拿着 profile 时不启动、不杀进程。"""
    launched: list[list[str]] = []
    probe = FullProbe(listeners=[], owners={999})
    browser = ConnectBrowser(cfg, probe=probe, launcher=lambda args: launched.append(args) or 1)
    with pytest.raises(BrowserOwnershipError):
        browser.ensure()
    assert launched == [], "profile 被占用时不许启动第二个浏览器"


def test_ensure_launch_early_exit_fails_without_retry(cfg):
    """启动进程提前退出 → 明确失败，且**只启动一次**（不无限重试）。"""
    launched: list[list[str]] = []
    probe = FullProbe(listeners=[], create_time=None)
    browser = ConnectBrowser(
        cfg, probe=probe,
        launcher=lambda args: launched.append(args) or 4242,
        clock=lambda: 0.0, sleeper=lambda s: None,
        )
    browser.config = BrowserConfig(exe=cfg.exe, profile_dir=cfg.profile_dir, launch_timeout_s=1.0)
    with pytest.raises(BrowserLaunchError):
        browser.ensure()
    assert len(launched) == 1


def test_ensure_launch_once_then_verified(cfg):
    """参数数组只含固定三项、且核验实际监听者后才算成功；第二次 ensure 直接复用。"""
    launched: list[list[str]] = []
    probe = FullProbe(listeners=[], exe=str(cfg.exe), cmdline=_cmdline(cfg))

    def launcher(args):
        launched.append(args)
        probe._listeners = [(5555, "127.0.0.1")]  # 模拟浏览器把 9222 绑起来
        return 5555

    ticks = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 1.0])
    browser = ConnectBrowser(cfg, probe=probe, launcher=launcher,
                             clock=lambda: next(ticks, 10.0), sleeper=lambda s: None)
    handle = browser.ensure()
    assert handle.launched is True and handle.pid == 5555
    assert launched == [cfg.launch_args()], "启动必须一次、参数必须是固定三项"

    again = browser.ensure()
    assert again.launched is False and again.detail == "reused"


def test_launch_args_are_loopback_only_and_no_dangerous_flags(cfg):
    args = cfg.launch_args()
    assert f"--remote-debugging-address=127.0.0.1" in args
    assert f"--remote-debugging-port={CDP_PORT}" in args
    assert f"--user-data-dir={cfg.profile_dir}" in args
    assert len(args) == 3
    joined = " ".join(args)
    for banned in ("--remote-allow-origins", "--no-sandbox", "0.0.0.0"):
        assert banned not in joined


# --------------------------------------------------------------------------- #
# 生产默认装配路径（R4：不只是注入全能 StubProbe）
# --------------------------------------------------------------------------- #
def test_boss_mode_assembles_browser_or_unavailable(monkeypatch, tmp_path):
    """`main.create_app` 在 boss 模式下必须把浏览器装进连接管理器。

    缺 `JOBPILOT_CHROME_*` 时装配 `UnavailableBrowser`：页面可用、连接安全拒绝。
    """
    from app import main as main_mod
    from app.adapters.fake import FakeAdapter

    cred = tmp_path / "cred"
    budget = tmp_path / "budget"
    (cred / "auth").mkdir(parents=True)
    budget.mkdir()
    (cred / "config.json").write_text(
        json.dumps({"request_budget_dir": str(budget)}), encoding="utf-8"
    )
    monkeypatch.setenv("ADAPTER", "boss")
    monkeypatch.setenv("JOBPILOT_BOSS_CRED_DIR", str(cred))
    monkeypatch.setenv("JOBPILOT_BOSS_BUDGET_DIR", str(budget))
    monkeypatch.delenv("JOBPILOT_CHROME_EXE", raising=False)
    monkeypatch.delenv("JOBPILOT_CHROME_PROFILE_DIR", raising=False)
    # 适配器替身：只验装配路径，不碰真实 CLI
    monkeypatch.setattr(main_mod, "BossAdapter", lambda cfg: FakeAdapter())

    app = main_mod.create_app(db_path=tmp_path / "fix3.db")
    browser = app.state.connect.browser
    assert browser is not None, "boss 模式必须装配浏览器能力（R4）"
    assert isinstance(browser, UnavailableBrowser)
    with pytest.raises(Exception) as excinfo:
        browser.ensure()
    assert "chrome" in str(excinfo.value)


def test_fake_mode_does_not_require_chrome_config(monkeypatch, tmp_path):
    """fake 模式不装配浏览器、不要求 Chrome 配置（离线语义不变）。"""
    from app import main as main_mod

    monkeypatch.delenv("ADAPTER", raising=False)
    monkeypatch.delenv("JOBPILOT_CHROME_EXE", raising=False)
    monkeypatch.delenv("JOBPILOT_CHROME_PROFILE_DIR", raising=False)

    app = main_mod.create_app(db_path=tmp_path / "fix3.db")
    assert app.state.connect.browser is None


def test_missing_chrome_config_blocks_connect_but_page_works(tmp_path, monkeypatch):
    """缺配置：连接安全拒绝（`browser_config:*`），状态接口与页面照常可用。"""
    from app.api.connect import build_manager

    cred = tmp_path / "cred"
    budget = tmp_path / "budget"
    (cred / "auth").mkdir(parents=True)
    budget.mkdir()
    (cred / "config.json").write_text(
        json.dumps({"request_budget_dir": str(budget)}), encoding="utf-8"
    )
    # `build_manager` 按 env 重算路径：必须显式给出，否则先撞 cred_env_missing
    monkeypatch.setenv("ADAPTER", "boss")
    monkeypatch.setenv("JOBPILOT_BOSS_CRED_DIR", str(cred))
    monkeypatch.setenv("JOBPILOT_BOSS_BUDGET_DIR", str(budget))

    class _Runner:
        def login(self, **kwargs):  # pragma: no cover - 不该被调用
            raise AssertionError("浏览器配置非法时不许进入登录")

    manager = build_manager(
        {"adapter": "boss", "adapter_options": {"data_dir": str(cred)}},
        tmp_path / "fix3.db",
        runner=_Runner(),
        browser=UnavailableBrowser("chrome_exe_missing"),
    )
    manager.start(confirm_bind=True)
    snap = manager.status()
    while snap["task_active"]:
        snap = manager.status()
    assert snap["state"] == "needs_user_action"
    assert snap["reason"].startswith("browser_config:")
    assert snap["task_active"] is False
    # 只读状态照常可用（页面不被拖垮）
    assert manager.status()["state"] == "needs_user_action"
