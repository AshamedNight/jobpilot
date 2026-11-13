"""纯离线验收：`search` / `detail` / `greet` 都不许碰真实 Boss CLI 与网络 —— 3 条。

为什么单开一组：前 96 条验的是"逻辑对不对"，这一组验的是**"它到底有没有出门"**。
两者不等价——测试可以全绿而实际上调用过真实上游（B 组前的 9 failed 事故里，就有 5 条
在 boss 适配器下**真的执行了**上游 `search`，花了预算写了 `request-budget.sqlite3`，
只是断言恰好也红了）。断言绿不绿，不证明没发请求。

三道探针，任一被触发就 `AssertionError`（实现集中在 `tests/offline_probe.py`）：

1. **CLI 探针**：`app.adapters.boss._cli_entry`（进程内调上游 click 的唯一入口）
2. **网络探针**：`socket.create_connection` + `socket.socket.connect`
3. **适配器探针**：`app.main.BossAdapter`（真适配器类本身不许被构造）

外加一条"库必须是临时库"的断言：跑测试顺手写进 `data/jobpilot.db` 也是一种越界。

网络探针的**安装时机**在 `offline_stack` 里：先进入 TestClient 再装、退 TestClient 之前
恢复（`finally` 里）。Windows `ProactorEventLoop` 的 self-pipe 走 `socket.socketpair()`，
其内部有一次真的 `socket.socket.connect`（连 `127.0.0.1`）—— 那是框架自己的初始化，
必须落在探针范围之外；**不是**给回环地址开白名单（被测代码连本机服务照样拦）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

#: 探针实现只有一份（`tests/offline_probe.py`）：诊断信息、两道硬阻断、socket 生命周期
from offline_probe import SocketTripwire, Tripwire, offline_stack

from app import models
from app.adapters.fake import JOBS as FAKE_JOBS
from app.adapters.fake import FakeAdapter
from app.main import create_app


@pytest.fixture()
def offline(tmp_path: Path, monkeypatch):
    """装好三只探针，并保证 `create_app` 造出来的是**临时库 + fake 适配器**。

    socket 探针的生命周期交给 `offline_stack`（审查条目三）：先建 app → 先进入
    TestClient（框架把事件循环初始化完）→ 再装网络探针 → 跑请求 → finally 恢复 →
    最后退 TestClient。两道硬阻断（CLI 入口 / 真适配器类）在进 TestClient **之前**
    就装好，一次都不放行。
    """
    import app.adapters.boss as boss_module
    import app.main as main_module

    cli_tripwire = Tripwire("真实 Boss CLI 入口 boss._cli_entry")
    socket_tripwire = SocketTripwire()
    boss_class_tripwire = Tripwire("真适配器类 app.main.BossAdapter")

    monkeypatch.setattr(boss_module, "_cli_entry", cli_tripwire)
    monkeypatch.setattr(main_module, "BossAdapter", boss_class_tripwire)

    app = create_app(db_path=tmp_path / "offline.db", env_path=tmp_path / "no.env")
    with offline_stack(app, sock=socket_tripwire) as client:
        yield client, cli_tripwire, socket_tripwire, boss_class_tripwire, tmp_path


def _assert_clean(cli, sock, boss_cls) -> None:
    """三只探针一次都没响——这才是"没出门"的证据。"""
    assert cli.hits == [], f"调用了真实 Boss CLI：{cli.hits}"
    assert sock.hits == [], f"发生了网络连接：{sock.hits}"
    assert boss_cls.hits == [], f"构造了真适配器：{boss_cls.hits}"


def test_sweep_is_fully_offline_with_temp_db(offline):
    """整轮 `sweep`（search → score → greet）：零 CLI、零网络、库在 tmp 里。"""
    client, cli, sock, boss_cls, tmp_path = offline
    adapter = client.app.state.adapter
    assert isinstance(adapter, FakeAdapter), "测试必须是本地桩适配器，不许是真适配器"

    # 库必须是临时库（跑测试顺手写进 data/jobpilot.db 也是越界）
    db_path = Path(str(client.app.state.db_path)).resolve()
    assert str(db_path).startswith(str(tmp_path.resolve()))
    assert "jobpilot.db" == db_path.name or db_path.name == "offline.db"

    sweep = client.post("/api/run/sweep").json()
    assert sweep["ok"] is True
    stats = sweep["stats"]
    assert stats["found"] == 8
    assert stats["greeted"] == 4

    # search 真的走了（桩适配器有记录），但不是走 CLI
    assert any(call.startswith("search:") for call in adapter.calls)
    assert any(call.startswith("greet:") for call in adapter.calls)
    _assert_clean(cli, sock, boss_cls)

    # 落库也只落在临时库
    conn = models.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 8
        assert conn.execute("SELECT COUNT(*) AS n FROM actions WHERE action_type='greet'").fetchone()["n"] == 4
    finally:
        conn.close()


class _StubAdapter(FakeAdapter):
    """本地桩：`search` 无 JD、`detail` 与 `greet` 都由桩自己回答，永不触达上游。"""

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
        return {"sent": True, "error_code": None, "detail": "桩：打招呼成功", "raw": {"code": 0}, "already_greeted": False}


def test_detail_and_greet_run_through_stub_not_cli(offline):
    """`detail` 与 `greet` 都真被走到过，但**全部由桩回答**，一个字节都没出网。"""
    client, cli, sock, boss_cls, _tmp_path = offline
    stub = _StubAdapter()
    client.app.state.adapter = stub
    client.app.state.adapter_name = "offline_stub"

    sweep = client.post("/api/run/sweep").json()
    assert sweep["ok"] is True
    stats = sweep["stats"]

    # 三个动作都真的发生了（否则"没出网"是废话——什么都没跑当然没出网）
    assert stats["enriched"] > 0, "detail 必须真的被调用过"
    assert stub.detail_calls, "桩的 detail 必须有调用记录"
    assert stats["greeted"] > 0, "greet 必须真的被调用过"
    assert stub.greet_calls, "桩的 greet 必须有调用记录"

    # 但它们一次都没有转去真实 CLI / 网络 / 真适配器
    _assert_clean(cli, sock, boss_cls)


def test_followup_and_exchange_are_offline(offline):
    """`followup` 链（chat → chatmsg → exchange → 抓号）同样零 CLI、零网络。"""
    client, cli, sock, boss_cls, _tmp_path = offline
    client.app.state.config["gate"]["exchange"] = "auto"

    client.post("/api/run/sweep")
    followup = client.post("/api/run/followup").json()
    assert followup["ok"] is True
    assert followup["stats"]["exchanged"] >= 1
    assert followup["stats"]["wechat_got"] >= 1

    _assert_clean(cli, sock, boss_cls)
