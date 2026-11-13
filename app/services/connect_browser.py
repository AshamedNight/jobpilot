"""专用 Chrome 的配置解析 / 归属核验 / 启动与复用（补充单 §3，B1–B7）。

硬边界：
- 只认**显式绝对路径**的本机配置：`JOBPILOT_CHROME_EXE`、`JOBPILOT_CHROME_PROFILE_DIR`；
  绝不接受 HTTP 请求体传入的路径或命令，绝不使用日常 Chrome 默认用户目录。
- CDP 固定 `http://127.0.0.1:9222`；启动参数用**参数数组**、`shell=False`，
  只允许 `--remote-debugging-address=127.0.0.1` / `--remote-debugging-port=9222` /
  `--user-data-dir=<已确认专用目录>`；禁止 `--remote-allow-origins=*`、公网监听、隧道。
- **归属核验**：端口在听、监听 PID 的 exe 与配置一致、命令行 `--user-data-dir` 与
  规范 profile 路径一致、CDP `/json/version` 有响应 —— 四者同时成立才复用；否则拒绝。
  不杀进程、不换端口、不删 profile 锁、不接管日常浏览器。
- 端口已开但归属不明 / 权限不足 / 非回环 → 拒绝（fail-closed），不猜。
- 启动等待有上界；浏览器提前退出或端口被抢 → 明确失败，不无限重试。

本模块只碰浏览器进程与 CDP 归属，**不含任何业务发送**。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: 固定 CDP 目标（不允许请求体覆盖、不允许换端口）。
CDP_HOST = "127.0.0.1"
CDP_PORT = 9222
CDP_URL = f"http://{CDP_HOST}:{CDP_PORT}"

#: 启动等待上界（秒）。
LAUNCH_TIMEOUT_S = 20.0
_POLL_INTERVAL_S = 0.25

#: 只允许这三个启动参数前缀（防"任意附加参数"）。
_ALLOWED_ARG_PREFIXES = (
    "--remote-debugging-address=",
    "--remote-debugging-port=",
    "--user-data-dir=",
)

#: 允许的**实际监听地址**（netstat 报回来的地址，不是命令行里声明的参数）。
_LOOPBACK_ADDRS = frozenset({"127.0.0.1", "::1", "localhost"})


class BrowserConfigError(Exception):
    """配置缺失/相对路径/日常 profile/不可执行 —— 一律拒绝，不猜。"""


class BrowserOwnershipError(Exception):
    """端口占用归属不明、PID 复用、权限不足、非回环 —— 一律拒绝接管。"""


class BrowserLaunchError(Exception):
    """启动失败/超时/端口被抢 —— 明确失败，不自动重试。"""


def _norm(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(Path(value).expanduser()))).rstrip("\\/")


@dataclass(frozen=True)
class ListenerInfo:
    """一个监听者：PID + **实际监听地址**（只核命令行参数证明不了真绑回环）。"""

    pid: int
    addr: str
    port: int


def _split_cmdline(text: str) -> list[str]:
    """按空白切命令行，但尊重双引号（Windows 路径常带空格/中文）。"""
    parts: list[str] = []
    buf: list[str] = []
    in_quote = False
    for ch in text or "":
        if ch == '"':
            in_quote = not in_quote
            continue
        if ch.isspace() and not in_quote:
            if buf:
                parts.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _parse_netstat(text: str, port: int) -> list[ListenerInfo]:
    """解析 `netstat -ano` 的 LISTENING 行（纯函数，便于离线桩测解析）。"""
    found: list[ListenerInfo] = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[0].upper() not in ("TCP", "TCPV6"):
            continue
        if parts[3].upper() != "LISTENING":
            continue
        pid_text = parts[4]
        if not pid_text.isdigit():
            continue
        addr, _, port_text = parts[1].rpartition(":")
        if not port_text.isdigit() or int(port_text) != port:
            continue
        found.append(ListenerInfo(pid=int(pid_text), addr=addr.strip("[]"), port=port))
    return found


@dataclass(frozen=True)
class BrowserConfig:
    exe: Path
    profile_dir: Path
    cdp_url: str = CDP_URL
    launch_timeout_s: float = LAUNCH_TIMEOUT_S

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BrowserConfig":
        source = os.environ if env is None else env
        exe_raw = (source.get("JOBPILOT_CHROME_EXE") or "").strip()
        profile_raw = (source.get("JOBPILOT_CHROME_PROFILE_DIR") or "").strip()
        if not exe_raw:
            raise BrowserConfigError("chrome_exe_missing")
        if not profile_raw:
            raise BrowserConfigError("chrome_profile_missing")
        exe = Path(exe_raw).expanduser()
        profile = Path(profile_raw).expanduser()
        if not exe.is_absolute():
            raise BrowserConfigError("chrome_exe_relative")
        # 【闭环 0 · 2026-11-09】"日常 profile" 先判，再判"是不是绝对路径"。
        #   原因：`Path("/home/u/.config/google-chrome/User Data").is_absolute()` 在 Windows 上是
        #   **False**（没有盘符），于是同一条配置在 Linux 上报"用了日常 profile"、
        #   在 Windows 上报"路径必须是绝对路径"——错误码随操作系统变，测试与排障都不可靠。
        #   而且"用了日常 profile"本身是更危险、更该明确告知的那一种。
        low = _norm(profile).replace("\\", "/").lower()
        if any(marker in low for marker in ("google/chrome/user data", "google-chrome/user data",
                                            "microsoft/edge/user data", "chromium/user data")):
            raise BrowserConfigError("chrome_profile_is_daily_default")
        if not profile.is_absolute():
            raise BrowserConfigError("chrome_profile_relative")
        if not exe.exists():
            raise BrowserConfigError("chrome_exe_not_found")
        return cls(exe=exe, profile_dir=profile)

    def launch_args(self) -> list[str]:
        """只允许固定的三个参数；顺序固定，不做字符串拼接。"""
        return [
            f"--remote-debugging-address={CDP_HOST}",
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={self.profile_dir}",
        ]


class PortProbe:
    """端口/进程/CDP 探测（**Windows 系统自带能力 + 标准库**，不依赖 psutil，不加新依赖）。

    实现边界（补充单 §3.2）：
    - 监听者：`netstat -ano`（参数数组、`shell=False`），解析 TCP/TCPV6 的 LISTENING 行，
      同时拿到**实际监听地址**与 PID —— 只核命令行参数不足以证明真的绑了回环。
    - 可执行文件：`kernel32.OpenProcess` + `QueryFullProcessImageNameW`（不引入第三方库）。
    - 命令行 / 创建时间：`powershell -NoProfile` 查 CIM `Win32_Process`。
      创建时间返回**原样字符串**（与 PID 一起做"同一进程"比对，避免本地化日期解析出错）。
    - 非 Windows 或任何一步失败 → 返回 `None`，由上层 fail-closed 拒绝接管。
    """

    def _run(self, argv: list[str]) -> str:
        proc = subprocess.run(  # noqa: S603 - 参数数组 + 系统自带可执行文件，无 shell
            argv, shell=False, capture_output=True, text=True, timeout=10, check=False
        )
        return proc.stdout or ""

    # -- 监听者 ---------------------------------------------------------- #
    def listeners(self, port: int) -> list[ListenerInfo]:
        try:
            return _parse_netstat(self._run(["netstat", "-ano"]), port)
        except (OSError, subprocess.SubprocessError):
            return []

    def listener_pids(self, port: int) -> set[int]:
        return {info.pid for info in self.listeners(port)}

    def listener_addresses(self, port: int) -> set[str]:
        return {info.addr for info in self.listeners(port)}

    # -- 进程信息 -------------------------------------------------------- #
    def process_exe(self, pid: int) -> str | None:
        if os.name != "nt":
            return None
        try:
            import ctypes  # noqa: PLC0415 - 仅 Windows 需要

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return None
            try:
                size = ctypes.c_uint(4096)
                buffer = ctypes.create_unicode_buffer(size.value)
                if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    return None
                return buffer.value or None
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - 探测失败一律视为"归属不明"
            return None

    def _cim(self, pid: int, prop: str) -> str | None:
        script = (
            f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}')"
            f".{prop}"
        )
        try:
            text = self._run([
                "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
            ]).strip()
        except (OSError, subprocess.SubprocessError):
            return None
        return text or None

    def process_cmdline(self, pid: int) -> list[str] | None:
        text = self._cim(pid, "CommandLine")
        if not text:
            return None
        return _split_cmdline(text)

    def process_create_time(self, pid: int) -> str | None:
        """进程创建时间（原样字符串）。与 PID 一起判定"是不是同一个进程"（防 PID 复用）。"""
        return self._cim(pid, "CreationDate")

    def processes_with_profile(self, profile_norm: str) -> set[int]:
        """谁在占用这个 profile（`--user-data-dir=<profile>`）—— 用于启动前占用检查。"""
        script = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains('--user-data-dir=') } | "
            "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
        )
        try:
            text = self._run([
                "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
            ])
        except (OSError, subprocess.SubprocessError):
            return set()
        owners: set[int] = set()
        for line in text.splitlines():
            pid_text, _, cmd = line.partition("\t")
            if not pid_text.strip().isdigit():
                continue
            for arg in _split_cmdline(cmd):
                if arg.startswith("--user-data-dir=") and _norm(arg.split("=", 1)[1]) == profile_norm:
                    owners.add(int(pid_text.strip()))
        return owners

    def cdp_version(self, url: str, timeout_s: float = 2.0) -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(f"{url}/json/version", timeout=timeout_s) as resp:  # noqa: S310
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            return payload if isinstance(payload, dict) else None
        except Exception:  # noqa: BLE001 - 探测失败一律视为"归属不明"
            return None


@dataclass
class BrowserHandle:
    launched: bool
    pid: int | None
    cdp_url: str
    detail: str
    started_at: str | None = None


class UnavailableBrowser:
    """配置不可用时的占位实现：页面照常可用，连接时**安全拒绝**（R4：缺配置不许崩）。"""

    def __init__(self, code: str = "chrome_config_unavailable") -> None:
        self.code = code

    def ensure(self) -> BrowserHandle:  # pragma: no cover - 恒抛
        raise BrowserConfigError(self.code)


class ConnectBrowser:
    """启动/复用专用 Chrome。所有外部动作都可注入（`probe` / `launcher`）。"""

    def __init__(
        self,
        config: BrowserConfig,
        *,
        probe: Any | None = None,
        launcher: Callable[[list[str]], int | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.probe = probe or PortProbe()
        self._launcher = launcher or self._default_launcher
        self._clock = clock
        self._sleep = sleeper

    # -- 启动器（参数数组、shell=False、无附加参数）-------------------------
    def _default_launcher(self, args: list[str]) -> int | None:
        assert all(any(a.startswith(p) for p in _ALLOWED_ARG_PREFIXES) for a in args)
        proc = subprocess.Popen(  # noqa: S603 - 参数数组 + 固定 exe，无 shell
            [str(self.config.exe), *args],
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.pid

    # -- 归属核验 -------------------------------------------------------- #
    def _listeners(self) -> list[ListenerInfo] | None:
        """有 `listeners()` 就用（能拿到实际监听地址）；老桩只有 `listener_pids()` 则返回 None。"""
        fn = getattr(self.probe, "listeners", None)
        if callable(fn):
            return list(fn(CDP_PORT))
        return None

    def verify_ownership(self) -> BrowserHandle | None:
        """返回可复用的句柄；不能证明归属则抛 `BrowserOwnershipError`。

        六项必须同时成立：监听 PID、PID 创建时间、目标 exe、规范 profile 路径、
        **实际监听地址是回环**、CDP 响应。少一项就拒绝接管（不杀进程、不换端口）。
        """
        infos = self._listeners()
        if infos is None:
            pids = set(self.probe.listener_pids(CDP_PORT))
            addresses: set[str] | None = None
        else:
            pids = {info.pid for info in infos}
            addresses = {info.addr for info in infos}
        if not pids:
            return None
        if len(pids) > 1:
            raise BrowserOwnershipError("多个进程同时监听 9222：归属不明")
        if addresses is not None and any(addr not in _LOOPBACK_ADDRS for addr in addresses):
            # 只看命令行参数证明不了真的绑了回环：这里核**实际监听地址**
            raise BrowserOwnershipError("监听地址不是回环：拒绝接管")
        pid = next(iter(pids))
        exe = self.probe.process_exe(pid)
        if exe is None:
            raise BrowserOwnershipError("读不到监听进程的可执行文件（权限不足）")
        if _norm(exe) != _norm(self.config.exe):
            raise BrowserOwnershipError("监听进程不是配置的专用 Chrome")
        # PID 复用防护：创建时间必须读得到（读不到 = 权限不足，fail-closed）
        create_time = None
        ctime_fn = getattr(self.probe, "process_create_time", None)
        if callable(ctime_fn):
            create_time = ctime_fn(pid)
            if not create_time:
                raise BrowserOwnershipError("读不到监听进程的创建时间（权限不足/PID 已复用）")
        cmdline = self.probe.process_cmdline(pid)
        if cmdline is None:
            raise BrowserOwnershipError("读不到监听进程命令行（权限不足）")
        expected = _norm(self.config.profile_dir)
        if not any(
            arg.startswith("--user-data-dir=")
            and _norm(arg.split("=", 1)[1]) == expected
            for arg in cmdline
        ):
            raise BrowserOwnershipError("监听进程的 profile 与配置不一致")
        if not any(arg == f"--remote-debugging-address={CDP_HOST}" for arg in cmdline):
            raise BrowserOwnershipError("监听进程不是只绑回环")
        version = self.probe.cdp_version(self.config.cdp_url)
        if not version:
            raise BrowserOwnershipError("CDP 无响应：无法证明这是本项目的浏览器")
        return BrowserHandle(launched=False, pid=pid, cdp_url=self.config.cdp_url,
                             detail="reused", started_at=create_time)

    # -- 复用 / 启动 ----------------------------------------------------- #
    def _profile_owners(self) -> set[int]:
        fn = getattr(self.probe, "processes_with_profile", None)
        if not callable(fn):
            return set()
        return set(fn(_norm(self.config.profile_dir)))

    def ensure(self) -> BrowserHandle:
        handle = self.verify_ownership()
        if handle is not None:
            return handle
        # 端口空闲 ≠ profile 空闲：别的进程可能正拿着这个 profile（关掉服务前留下的）
        owners = self._profile_owners()
        if owners:
            raise BrowserOwnershipError("专用 profile 已被其他进程占用：拒绝启动、不杀进程")
        # 端口空闲 → 启动本项目专用 Chrome，并**核验实际监听者**（不信 Popen.pid）
        launched_pid = self._launcher(self.config.launch_args())
        deadline = self._clock() + self.config.launch_timeout_s
        while self._clock() < deadline:
            self._sleep(_POLL_INTERVAL_S)
            if launched_pid is not None:
                # 启动进程提前退出 → 明确失败，不无限重试
                alive_fn = getattr(self.probe, "process_create_time", None)
                if callable(alive_fn) and alive_fn(launched_pid) is None:
                    if self._listeners() in (None, []) and not self.probe.listener_pids(CDP_PORT):
                        raise BrowserLaunchError("启动的浏览器提前退出：未进入登录")
            try:
                handle = self.verify_ownership()
            except BrowserOwnershipError:
                handle = None
            if handle is not None:
                return BrowserHandle(launched=True, pid=handle.pid, cdp_url=handle.cdp_url,
                                     detail="launched", started_at=handle.started_at)
        raise BrowserLaunchError("启动超时或端口被抢：未进入登录")
