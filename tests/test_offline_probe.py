"""离线探针自身的契约 —— 7 条（只验探针，不验业务逻辑）。

为什么单开一组：fix4 / fix5 的「零网络探针」在 Windows 11 + Python 3.11 的
`ProactorEventLoop` 下**误伤了测试框架自己** —— 事件循环的 self-pipe 走
`socket.socketpair()`，而 CPython 在 Windows 上的实现里有一句真的
`sock.connect(a.getsockname())`（连 `127.0.0.1`）。探针当时装在 `TestClient`
进入之前、活到 `TestClient` 退出之后，于是"框架自己的初始化"被当成"被测代码在出网"。

修法是**改安装时机**，不是给回环地址开白名单（一旦白名单，被测代码连本机服务也能溜出去）。
本文件把这件事钉成契约：

| # | 测试 | 钉住什么 |
|---|---|---|
| 1 | `test_probe_reports_entry_and_target_address` | 诊断里有调用入口 + 目标地址:端口 |
| 2 | `test_probe_reports_family_type_proto` | 诊断里有 family / type / proto |
| 3 | `test_probe_reports_call_stack_and_origin` | 诊断里有调用栈，且能判出"测试侧 / 框架内部 / 被测代码" |
| 4 | `test_probe_blocks_before_any_real_connection` | 探针拦在握手之前，监听端 accept 不到任何连接 |
| 5 | `test_testclient_lifecycle_sits_outside_socket_probe` | 进/退 `TestClient` 时探针**没装**，只有请求窗口内装着 |
| 6 | `test_socket_probe_restored_in_finally_before_testclient_exit` | 测试体炸掉时也先恢复 socket、再退 `TestClient` |
| 7 | `test_hard_blocks_survive_the_socket_probe_rework` | 两道硬阻断没被弱化，且桩真的跑过 search/detail/greet |
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import offline_probe
from app.adapters.fake import JOBS as FAKE_JOBS
from app.adapters.fake import FakeAdapter
from app.main import create_app

#: 文档保留段（RFC 5737），保证就算探针漏了也不可能真的出去
_NOWHERE_V4 = "203.0.113.7"


def _probe_is_armed() -> bool:
    """探针是否正装着 —— 用身份比较，不靠自report。"""
    return socket.socket.connect is not offline_probe.ORIGINAL_SOCKET_CONNECT


# --------------------------------------------------------------------------- #
# 条目一：诊断信息
# --------------------------------------------------------------------------- #
def test_probe_reports_entry_and_target_address():
    """`create_connection` 被拦时，错误里必须能看出**哪个入口、连了哪里**。"""
    sock = offline_probe.SocketTripwire()
    with offline_probe.socket_probe(sock):
        with pytest.raises(AssertionError) as exc:
            socket.create_connection((_NOWHERE_V4, 8010))

    message = str(exc.value)
    assert "socket.create_connection" in message, message
    assert f"{_NOWHERE_V4}:8010" in message, message
    assert "网络探针" in message, "错误信息必须点名是哪只探针触发的"
    # 记了账（既有 `sock.hits == []` 断言依赖这个字段）
    assert len(sock.hits) == 1
    assert sock.hits[0][0] == "socket.create_connection"
    assert sock.diagnostics == [message]


def test_probe_reports_family_type_proto():
    """`socket.socket.connect` 被拦时，必须带上 family / type / proto 三个字段。"""
    sock = offline_probe.SocketTripwire()
    with offline_probe.socket_probe(sock):
        # proto 要显式传，否则 socket.socket 的 proto 属性是 0（不是 IPPROTO_TCP）
        candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        try:
            with pytest.raises(AssertionError) as exc:
                candidate.connect(("198.51.100.9", 443))
        finally:
            candidate.close()

    message = str(exc.value)
    assert "socket.socket.connect" in message, message
    assert "198.51.100.9:443" in message, message
    assert "family" in message and "AF_INET" in message, message
    assert "type" in message and "SOCK_STREAM" in message, message
    assert "proto" in message and "IPPROTO_TCP" in message, message
    # family / type / proto 取自被拦的那个 socket 对象本身
    assert "AF_INET(2)" in message and "SOCK_STREAM(1)" in message and "IPPROTO_TCP(6)" in message


def test_probe_reports_call_stack_and_origin():
    """探针必须留下调用栈，且能判出这一枪是**测试侧**打的（不是被测代码）。"""
    sock = offline_probe.SocketTripwire()
    with offline_probe.socket_probe(sock):
        with pytest.raises(AssertionError) as exc:
            socket.create_connection((_NOWHERE_V4, 22))

    message = str(exc.value)
    assert "调用栈" in message, message
    # 本测试函数必须出现在栈里 —— 否则"保留了栈帧"就是空话
    assert "test_probe_reports_call_stack_and_origin" in message, message
    assert "test_offline_probe.py" in message, message
    # 来源判定：三选一必须真的判出来，而不是恒定输出
    assert "来源判定" in message, message
    assert "测试侧" in message, message
    assert "boss 适配器帧: 否" in message, message


# --------------------------------------------------------------------------- #
# 条目一（续）：不许真的建立连接
# --------------------------------------------------------------------------- #
def test_probe_blocks_before_any_real_connection():
    """本地开一个监听口，两条网络入口各撞一次探针 → 监听端**accept 不到任何连接**。

    这条是"不许真的建立连接"的正向证据：如果探针只是"记一笔再放行"，
    accept 会立刻拿到一条已完成握手的连接，测试变红。
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    target = listener.getsockname()
    try:
        sock = offline_probe.SocketTripwire()
        with offline_probe.socket_probe(sock):
            with pytest.raises(AssertionError):
                socket.create_connection(target, timeout=1)
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                with pytest.raises(AssertionError):
                    raw.connect(target)
            finally:
                raw.close()
        assert len(sock.hits) == 2, sock.hits

        # 握手从未发生：监听队列里没有任何"待接受"的连接
        listener.settimeout(0.5)
        with pytest.raises(socket.timeout):
            listener.accept()
    finally:
        listener.close()


# --------------------------------------------------------------------------- #
# 条目三：探针生命周期
# --------------------------------------------------------------------------- #
class _SpyClient(TestClient):
    """在 `__enter__` / `__exit__` 两个时刻记录"探针装没装"。"""

    def __init__(self, app, events: list) -> None:
        super().__init__(app)
        self._events = events

    def __enter__(self):
        self._events.append(("enter", _probe_is_armed()))
        return super().__enter__()

    def __exit__(self, *exc_info):
        self._events.append(("exit", _probe_is_armed()))
        return super().__exit__(*exc_info)


def test_testclient_lifecycle_sits_outside_socket_probe(tmp_path: Path, monkeypatch):
    """顺序契约：进 TestClient → 装探针 → 请求 → 恢复 → 退 TestClient。

    期望事件序列 = `enter(未装) / request(装着) / exit(未装)`。
    fix5 的顺序（先装探针再进 TestClient）会得到 `enter(装着) / exit(装着)` → 红。
    """
    events: list = []
    monkeypatch.setattr(offline_probe, "TestClient", lambda app: _SpyClient(app, events))
    sock = offline_probe.SocketTripwire()
    app = create_app(
        db_path=tmp_path / "lifecycle.db", env_path=tmp_path / "no.env"
    )

    with offline_probe.offline_stack(app, sock=sock) as client:
        events.append(("request", _probe_is_armed()))
        assert client.get("/api/status").status_code == 200

    assert events == [("enter", False), ("request", True), ("exit", False)], (
        f"探针覆盖了 TestClient 的生命周期：{events}"
    )
    assert sock.hits == [], f"被测请求期间探针误响：{sock.hits}"
    # 收工后必须是"原始"的 connect，不许残留
    assert socket.socket.connect is offline_probe.ORIGINAL_SOCKET_CONNECT
    assert socket.create_connection is offline_probe.ORIGINAL_CREATE_CONNECTION


def test_socket_probe_restored_in_finally_before_testclient_exit(
    tmp_path: Path, monkeypatch
):
    """测试体抛异常时：socket 也必须先恢复、再退 TestClient（恢复动作在 finally 里）。"""
    events: list = []
    monkeypatch.setattr(offline_probe, "TestClient", lambda app: _SpyClient(app, events))
    sock = offline_probe.SocketTripwire()
    app = create_app(db_path=tmp_path / "boom.db", env_path=tmp_path / "no.env")

    with pytest.raises(RuntimeError, match="测试体故意炸"):
        with offline_probe.offline_stack(app, sock=sock):
            events.append(("request", _probe_is_armed()))
            raise RuntimeError("测试体故意炸")

    assert events == [("enter", False), ("request", True), ("exit", False)], (
        f"异常路径下探针顺序错乱：{events}"
    )
    assert sock.hits == []
    assert socket.socket.connect is offline_probe.ORIGINAL_SOCKET_CONNECT
    assert socket.create_connection is offline_probe.ORIGINAL_CREATE_CONNECTION


# --------------------------------------------------------------------------- #
# 条目二：两道硬阻断没被弱化
# --------------------------------------------------------------------------- #
class _StubAdapter(FakeAdapter):
    """本地桩：`search` 不带 JD，`detail` 给正文，`greet` 记账并成功。"""

    def __init__(self) -> None:
        super().__init__()
        self._jd = {str(job["job_id"]): str(job.get("jd_text") or "") for job in FAKE_JOBS}
        self.detail_calls: list[str] = []
        self.greet_calls: list[str] = []

    def probe(self) -> set[str]:
        self.calls.append("probe")
        return set(self.capabilities)

    def search(self, query: str = "", **filters) -> list[dict]:
        self.calls.append(f"search:{query}")
        return [dict(job, jd_text="") for job in self._jobs]

    def detail(self, security_id: str, job_id: str | None = None) -> dict:
        self.detail_calls.append(str(job_id))
        return {
            "ok": True,
            "job_id": str(job_id or ""),
            "jd_text": self._jd.get(str(job_id), ""),
            "address": "",
            "hr_active": "3d",
            "greeted": False,
        }

    def greet(self, security_id: str, job_id: str, message: str) -> dict:
        self.greet_calls.append(str(job_id))
        return {
            "sent": True,
            "error_code": None,
            "detail": "桩：打招呼成功",
            "raw": {"code": 0},
            "already_greeted": False,
        }


def test_hard_blocks_survive_the_socket_probe_rework(tmp_path: Path, monkeypatch):
    """改 socket 生命周期**不许**顺手把两道硬阻断拆掉。

    左半边：整轮 sweep 由本地桩回答 —— `search` / `detail` / `greet` 都真的执行了，
    同时三只探针一次没响（"什么都没跑"当然不会出网，所以先证明真的跑了）。
    右半边：CLI 入口 / 真适配器类仍然一碰就炸。
    """
    import app.adapters.boss as boss_module
    import app.main as main_module

    cli = offline_probe.Tripwire("真实 Boss CLI 入口 boss._cli_entry")
    boss_cls = offline_probe.Tripwire("真适配器类 app.main.BossAdapter")
    monkeypatch.setattr(boss_module, "_cli_entry", cli)
    monkeypatch.setattr(main_module, "BossAdapter", boss_cls)

    sock = offline_probe.SocketTripwire()
    app = create_app(db_path=tmp_path / "hard_blocks.db", env_path=tmp_path / "no.env")

    with offline_probe.offline_stack(app, sock=sock) as client:
        stub = _StubAdapter()
        client.app.state.adapter = stub
        client.app.state.adapter_name = "hard_block_stub"

        result = client.post("/api/run/sweep").json()
        assert result["ok"] is True
        stats = result["stats"]
        # 三个动作真的发生了
        assert stats["enriched"] == 4, stats
        assert sorted(stub.detail_calls) == ["job_001", "job_002", "job_003", "job_008"]
        assert stats["greeted"] == 4, stats
        assert len(stub.greet_calls) == 4
        assert any(call.startswith("search:") for call in stub.calls)
        # 但它们一次都没有转去真实 CLI / 网络 / 真适配器
        assert cli.hits == [], cli.hits
        assert sock.hits == [], sock.hits
        assert boss_cls.hits == [], boss_cls.hits

    # 硬阻断仍在岗：真调用一次即可证明（不是靠"没人调"静默通过）
    with pytest.raises(AssertionError, match="boss._cli_entry"):
        boss_module._cli_entry(["--json", "search", "法务"])
    with pytest.raises(AssertionError, match="BossAdapter"):
        main_module.BossAdapter()
    assert len(cli.hits) == 1 and len(boss_cls.hits) == 1
