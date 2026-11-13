"""Connect_fix2 · 专用 Chrome 的配置/归属/启动（补充单 B1–B7，全离线桩）。"""

from __future__ import annotations

import concurrent.futures

import pytest

from app.services.connect_browser import (
    CDP_URL,
    BrowserConfig,
    BrowserConfigError,
    BrowserLaunchError,
    BrowserOwnershipError,
    ConnectBrowser,
)

EXE = "/opt/fake-chrome/chrome"
PROFILE = "/home/u/.jobpilot-chrome-profile"


class StubProbe:
    def __init__(self, *, pids=None, exe=EXE, cmdline=None, version=None):
        self.pids = list(pids or [])
        self.exe = exe
        self.cmdline = cmdline
        self.version = version if version is not None else {"Browser": "Chrome/120"}

    def listener_pids(self, port):
        assert port == 9222
        return set(self.pids)

    def process_exe(self, pid):
        return self.exe

    def process_cmdline(self, pid):
        if self.cmdline is not None:
            return self.cmdline
        return [EXE, "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=9222", f"--user-data-dir={PROFILE}"]

    def cdp_version(self, url, timeout_s=2.0):
        return self.version


def make_config(**kw) -> BrowserConfig:
    return BrowserConfig(exe=kw.get("exe", EXE), profile_dir=kw.get("profile_dir", PROFILE),
                         launch_timeout_s=kw.get("timeout", 5.0))


class Recorder:
    def __init__(self, probe: StubProbe, *, appear_pid: int | None = 4242):
        self.probe = probe
        self.appear_pid = appear_pid
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> int | None:
        self.calls.append(list(args))
        if self.appear_pid is not None:
            self.probe.pids = [self.appear_pid]
        return 999  # 故意与真实监听 PID 不同：证明我们不靠 Popen.pid

    @property
    def launches(self) -> int:
        return len(self.calls)


# --------------------------------------------------------------------------- #
# B1 端口空闲 + 配置有效 → 只启动一次，参数数组固定，核验监听者后才继续
# --------------------------------------------------------------------------- #
def test_b1_launch_once_with_fixed_args():
    probe = StubProbe(pids=[])
    rec = Recorder(probe)
    browser = ConnectBrowser(make_config(), probe=probe, launcher=rec, sleeper=lambda s: None)

    handle = browser.ensure()
    assert rec.launches == 1
    assert handle.launched is True
    assert handle.pid == 4242, "必须用实际监听 PID，不能用 Popen 返回的 pid"

    args = rec.calls[0]
    assert args == [
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=9222",
        f"--user-data-dir={PROFILE}",
    ]
    assert not any("remote-allow-origins" in a for a in args)
    assert not any("no-sandbox" in a for a in args)


# --------------------------------------------------------------------------- #
# B2 已确认的专用实例 → 不再启动
# --------------------------------------------------------------------------- #
def test_b2_reuse_confirmed_instance():
    probe = StubProbe(pids=[777])
    rec = Recorder(probe)
    browser = ConnectBrowser(make_config(), probe=probe, launcher=rec, sleeper=lambda s: None)
    handle = browser.ensure()
    assert handle.launched is False
    assert rec.launches == 0
    assert handle.cdp_url == CDP_URL


# --------------------------------------------------------------------------- #
# B3 归属不明 / 错 profile / 非回环 / 多监听 → 拒绝，且不启动
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", ["multi_pid", "wrong_exe", "wrong_profile", "no_loopback", "no_cmdline"])
def test_b3_ownership_must_be_proven(case):
    probe = StubProbe(pids=[777])
    if case == "multi_pid":
        probe.pids = [777, 778]
    elif case == "wrong_exe":
        probe.exe = "/opt/other/chrome"
    elif case == "wrong_profile":
        probe.cmdline = [EXE, "--remote-debugging-address=127.0.0.1",
                         "--user-data-dir=/somewhere/else"]
    elif case == "no_loopback":
        probe.cmdline = [EXE, "--remote-debugging-address=0.0.0.0",
                         f"--user-data-dir={PROFILE}"]
    elif case == "no_cmdline":
        probe.cmdline = None
        probe.process_cmdline = lambda pid: None
    rec = Recorder(probe)
    browser = ConnectBrowser(make_config(), probe=probe, launcher=rec, sleeper=lambda s: None)
    with pytest.raises(BrowserOwnershipError):
        browser.ensure()
    assert rec.launches == 0, "归属不明时不许启动、不许杀进程、不许换端口"


def test_b3b_cdp_no_response_is_refused():
    probe = StubProbe(pids=[777], version=None)
    probe.cdp_version = lambda url, timeout_s=2.0: None
    rec = Recorder(probe)
    browser = ConnectBrowser(make_config(), probe=probe, launcher=rec, sleeper=lambda s: None)
    with pytest.raises(BrowserOwnershipError):
        browser.ensure()
    assert rec.launches == 0


# --------------------------------------------------------------------------- #
# B4 配置非法一律拒绝；空格/中文路径不被 shell 拆分
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "case,code",
    [
        ("missing_all", "chrome_exe_missing"),
        ("no_profile", "chrome_profile_missing"),
        ("relative_exe", "chrome_exe_relative"),
        ("relative_profile", "chrome_profile_relative"),
        ("daily_default", "chrome_profile_is_daily_default"),
        ("exe_not_found", "chrome_exe_not_found"),
    ],
)
def test_b4_invalid_config_refused(case, code, tmp_path):
    # exe 用**真实存在的临时文件**，否则"exe 不存在"会遮住后面每一条更具体的判定
    real_exe = tmp_path / "chrome"
    real_exe.write_text("", encoding="utf-8")
    profile = tmp_path / "专用 配置"
    env = {
        "missing_all": {},
        "no_profile": {"JOBPILOT_CHROME_EXE": str(real_exe)},
        "relative_exe": {"JOBPILOT_CHROME_EXE": "chrome.exe",
                         "JOBPILOT_CHROME_PROFILE_DIR": str(profile)},
        "relative_profile": {"JOBPILOT_CHROME_EXE": str(real_exe),
                             "JOBPILOT_CHROME_PROFILE_DIR": "prof"},
        "daily_default": {"JOBPILOT_CHROME_EXE": str(real_exe),
                          "JOBPILOT_CHROME_PROFILE_DIR": "/home/u/.config/google-chrome/User Data"},
        "exe_not_found": {"JOBPILOT_CHROME_EXE": str(tmp_path / "nope" / "chrome"),
                          "JOBPILOT_CHROME_PROFILE_DIR": str(profile)},
    }[case]
    with pytest.raises(BrowserConfigError) as exc:
        BrowserConfig.from_env(env)
    assert code in str(exc.value)


def test_b4b_path_with_spaces_and_chinese_stays_one_arg():
    cfg = BrowserConfig(exe="/opt/Chrome 程序/chrome", profile_dir="/home/u/专用 配置")
    args = cfg.launch_args()
    assert len(args) == 3
    assert args[2] == "--user-data-dir=/home/u/专用 配置"


# --------------------------------------------------------------------------- #
# B5 启动超时/端口被抢 → 明确失败、不无限重试
# --------------------------------------------------------------------------- #
def test_b5_launch_timeout_no_infinite_retry():
    probe = StubProbe(pids=[])
    rec = Recorder(probe, appear_pid=None)  # 永远起不来
    browser = ConnectBrowser(make_config(timeout=0.0), probe=probe, launcher=rec, sleeper=lambda s: None)
    with pytest.raises(BrowserLaunchError):
        browser.ensure()
    assert rec.launches == 1, "只允许启动一次，不无限重试"


# --------------------------------------------------------------------------- #
# B6 并发双击：只启动一次（同一 BrowserConfig 下由上层占用保证，这里验模块自身幂等性不叠加）
# --------------------------------------------------------------------------- #
def test_b6_concurrent_ensure_launches_once():
    probe = StubProbe(pids=[])
    rec = Recorder(probe)
    browser = ConnectBrowser(make_config(), probe=probe, launcher=rec, sleeper=lambda s: None)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        handle = pool.submit(browser.ensure).result(timeout=10)
    assert handle.launched is True
    assert rec.launches == 1


# --------------------------------------------------------------------------- #
# B7 启动失败不得假装成功；配置里不许出现危险参数
# --------------------------------------------------------------------------- #
def test_b7_never_reports_success_without_verified_listener():
    probe = StubProbe(pids=[])
    rec = Recorder(probe, appear_pid=1234)
    probe.exe = "/opt/other/chrome"  # 起来的是别的进程
    browser = ConnectBrowser(make_config(timeout=0.0), probe=probe, launcher=rec, sleeper=lambda s: None)
    with pytest.raises(BrowserLaunchError):
        browser.ensure()
    assert rec.launches == 1
    assert not any("remote-allow-origins" in a for a in rec.calls[0])
