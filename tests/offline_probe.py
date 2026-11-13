"""离线验收探针的唯一实现 —— `test_offline_guarantee` / `test_offline_gates` /
`test_offline_probe` 三份共用（本模块不叫 `test_*`，pytest 不会收集它）。

审查条目一 / 二 / 三 / 四 落地在这一个文件里：

1. **诊断信息**（条目一）：探针被触发时一次性拼出「调用入口 / 目标地址与端口 /
   family / type / proto / 来源判定 / 调用栈」，并让错误信息明确点名是哪个探针。
   只在触发时拼（`_build_message` 只在 `_fire` 里调用），正常通过零输出。
2. **两道硬阻断**（条目二）：`Tripwire`（CLI 入口 / 真适配器类）与 socket 探针是
   两套独立机制，改 socket 生命周期不触碰前者。
3. **生命周期**（条目三）：见 `offline_stack` —— 先建 app → 先进入 TestClient（让框架
   完成事件循环初始化）→ 再装 socket 探针 → 跑被测请求 → `finally` 里先恢复 socket
   → 最后才退出 TestClient。
4. **不做回环例外**（条目四）：`127.0.0.1` / `::1` 与外部地址一视同仁，一律拦。
   误伤的根因是「安装时机」而不是「地址判定」，所以这里不设任何白名单。
"""

from __future__ import annotations

import contextlib
import socket
import traceback
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

#: import 时抓的"原始"引用（此刻还没有任何 monkeypatch）。测试靠身份比较判断
#: "探针现在到底装没装"——这是条目三能被断言的地方。
ORIGINAL_SOCKET_CONNECT = socket.socket.connect
ORIGINAL_CREATE_CONNECTION = socket.create_connection

#: 栈最多保留多少帧（够看清 pytest 内部 → 被测代码的全链路，又不至于刷屏）
MAX_STACK_FRAMES = 12

_PROTO_NAMES = {
    0: "IPPROTO_IP",
    6: "IPPROTO_TCP",
    17: "IPPROTO_UDP",
    41: "IPPROTO_IPV6",
}

_NOT_AVAILABLE = "不可得"


class Tripwire:
    """硬阻断探针：被调用即失败（真实 Boss CLI 入口 / 真适配器类）。

    与 socket 探针**互不依赖**：改 socket 生命周期时这个类一行没动。
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.hits: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.hits.append((args, kwargs))
        raise AssertionError(
            f"纯离线验收失败：{self.label} 被调用了（本组测试不许出网、不许碰上游）"
        )

    def __getattr__(self, name):
        # 探针自身被当成对象使用时（如 `.probe()`）也不许放过
        raise AssertionError(f"纯离线验收失败：{self.label} 的属性 {name!r} 被访问了")


def _enum_text(value: Any) -> str:
    """`AddressFamily.AF_INET` → `AF_INET(2)`；拿不到就原样 repr。"""
    if isinstance(value, str):
        return value
    name = getattr(value, "name", None)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return repr(value)
    return f"{name}({number})" if name else str(number)


def _proto_text(proto: Any) -> str:
    if isinstance(proto, str):
        return proto
    try:
        number = int(proto)
    except (TypeError, ValueError):
        return repr(proto)
    return f"{_PROTO_NAMES.get(number, 'proto')}({number})"


def _address_text(address: Any) -> str:
    if isinstance(address, tuple) and len(address) >= 2:
        return f"{address[0]}:{address[1]}"
    return repr(address)


def _normalize(path: Any) -> str:
    return str(path).replace("\\", "/")


def _classify(frames) -> str:
    """判断这次触发到底来自**测试框架内部**、**被测 jobpilot 代码**，还是 **Boss CLI / 适配器**。

    从最内层（离触发点最近）的帧往外找第一个"不在本文件里"的帧，按它的路径定位。
    """
    for frame in reversed(frames):
        filename = _normalize(frame.filename)
        if Path(filename).name == "offline_probe.py":
            continue
        if "/app/adapters/" in filename:
            return f"Boss CLI / 适配器路径（{filename}:{frame.lineno} in {frame.name}）"
        stripped = filename.lstrip("./")
        if "/tests/" in filename or Path(stripped).name.startswith("test_"):
            return f"测试侧（{filename}:{frame.lineno} in {frame.name}）"
        if "/app/" in filename:
            return f"被测 jobpilot 生产代码（{filename}:{frame.lineno} in {frame.name}）"
        if "site-packages" in filename or "_pytest" in filename:
            return (
                "测试框架内部 pytest / TestClient / asyncio"
                f"（{filename}:{frame.lineno} in {frame.name}）"
            )
    return "未识别（栈帧全部落在探针自身）"


def _build_message(entry: str, address: Any, family: Any, type_: Any, proto: Any) -> str:
    frames = traceback.extract_stack()[:-1]
    tail = frames[-MAX_STACK_FRAMES:]
    stack_text = "\n".join(
        f"    [{index:02d}] {_normalize(frame.filename)}:{frame.lineno} in {frame.name}"
        for index, frame in enumerate(tail)
    )
    boss_frame = any("/app/adapters/boss.py" in _normalize(f.filename) for f in tail)
    return (
        f"纯离线验收失败：网络探针触发 —— {entry}\n"
        f"  探针         : 网络探针（socket），本组测试不许出网、不许碰上游\n"
        f"  调用入口     : {entry}\n"
        f"  目标地址     : {_address_text(address)}\n"
        f"  family       : {_enum_text(family)}\n"
        f"  type         : {_enum_text(type_)}\n"
        f"  proto        : {_proto_text(proto)}\n"
        f"  来源判定     : {_classify(tail)}\n"
        f"  boss 适配器帧: {'是' if boss_frame else '否'}\n"
        f"  调用栈（外 → 内，末帧 = 触发点）:\n{stack_text}"
    )


class SocketTripwire(Tripwire):
    """网络探针：`socket.create_connection` + `socket.socket.connect`。

    被触发时**先记账、再带诊断抛错**，绝不真的建立连接（不调原始实现）。
    """

    label = "网络连接 socket.create_connection / socket.socket.connect"

    def __init__(self) -> None:
        super().__init__(self.label)
        self.diagnostics: list[str] = []

    @property
    def armed(self) -> bool:
        return socket.socket.connect is not ORIGINAL_SOCKET_CONNECT

    def _fire(self, entry: str, address: Any, family: Any, type_: Any, proto: Any):
        message = _build_message(entry, address, family, type_, proto)
        self.hits.append((entry, _address_text(address)))
        self.diagnostics.append(message)
        raise AssertionError(message)

    def on_create_connection(self, *args, **kwargs):
        address = args[0] if args else kwargs.get("address")
        # `create_connection` 内部才建 socket，这一层拿不到 family/type/proto
        self._fire(
            "socket.create_connection",
            address,
            f"{_NOT_AVAILABLE}（本层无 socket 对象，由 create_connection 内部自建）",
            f"{_NOT_AVAILABLE}（同上）",
            f"{_NOT_AVAILABLE}（同上）",
        )

    def on_connect(self, sock_obj, *args, **kwargs):
        address = args[0] if args else kwargs.get("address")
        try:
            family, type_, proto = sock_obj.family, sock_obj.type, sock_obj.proto
        except Exception:  # pragma: no cover - 只有非 socket 对象才会到这里
            family = type_ = proto = f"{_NOT_AVAILABLE}（非 socket 对象）"
        self._fire("socket.socket.connect", address, family, type_, proto)


def install_socket_probe(tripwire: SocketTripwire) -> pytest.MonkeyPatch:
    """装探针。返回的 `MonkeyPatch` 由调用方决定何时 `undo()`（= 何时恢复）。"""
    def connect_probe(sock_obj, *args, **kwargs):
        # 必须是**普通函数**：绑定方法不是描述符，实例调用时 self 不会传进来，
        # 那样就拿不到 family / type / proto（第一版踩过）。
        tripwire.on_connect(sock_obj, *args, **kwargs)

    patch = pytest.MonkeyPatch()
    patch.setattr(socket, "create_connection", tripwire.on_create_connection)
    patch.setattr(socket.socket, "connect", connect_probe)
    return patch


@contextlib.contextmanager
def socket_probe(tripwire: SocketTripwire):
    """探针只在 `with` 块内生效；**异常时也一定恢复**（恢复写在 finally 里）。"""
    patch = install_socket_probe(tripwire)
    try:
        yield tripwire
    finally:
        patch.undo()


@contextlib.contextmanager
def offline_stack(app, *, sock: SocketTripwire):
    """离线验收的**顺序契约**（审查条目三）：

        ① 建 app（调用方先建好，传进来）
        ② 先进入 TestClient —— 让 pytest / TestClient / 事件循环把初始化做完
        ③ 再装 socket 探针
        ④ 跑被测请求
        ⑤ finally 里先恢复 socket
        ⑥ 最后才退出 TestClient

    这样 Windows 上 `ProactorEventLoop` 的 self-pipe（`socket.socketpair()` 内部真的会
    调一次 `socket.socket.connect`）发生在探针**范围之外**，不需要为回环地址开白名单；
    而被测请求照样全被覆盖。
    """
    client = TestClient(app)
    client.__enter__()
    try:
        with socket_probe(sock):
            yield client
    finally:
        # 走到这里 socket 已经恢复（上一行的 with 已退出），再退 TestClient
        client.__exit__(None, None, None)
