"""上游 CLI 超时（`adapter_options.timeout_s`）。

2026-11-04 真机事故：`timeout_s` 在 `BossAdapter.__init__` 里赋值后**零引用**，
于是上游一次卡住 = 这个进程永远等下去（实测一轮 pass 卡了 10 分 24 秒，
`/api/pacing/status` 的 `last_verdict` 全程 null）。

现在的要求有两条，缺一不可：
1. 到点必须返回 `TIMEOUT`（不许无限等）
2. 超时后**本进程不再调用上游**（fail-closed）。理由：卡住的线程还握着
   `_CLI_LOCK` 和进程全局的 `sys.stdout`，再发只会排队到超时，把"卡死"伪装成"很慢"。
"""

from __future__ import annotations

import time

import pytest

from app.adapters import boss as B


@pytest.fixture(autouse=True)
def _clean_cli_state():
    """`_CLI_STUCK` 与 `_CLI_LOCK` 都是模块级状态：不隔离会把后面所有 boss 用例一起带红。

    上一个用例故意留下的"卡死线程"会握着锁约 1 秒，所以这里除了复位标记，
    还要**等领域锁释放**——否则后面的用例拿到的是 `lock_timeout`，测的就不是超时了。
    """
    B.reset_cli_state()
    assert B._CLI_LOCK.acquire(timeout=5.0), "上一个用例卡住的线程还握着 CLI 锁"
    B._CLI_LOCK.release()
    yield
    B.reset_cli_state()


def _adapter(tmp_path) -> B.BossAdapter:
    return B.BossAdapter({"adapter_options": {"data_dir": str(tmp_path), "timeout_s": 0.2}})


def test_cli_timeout_returns_timeout_envelope(tmp_path, monkeypatch):
    """`timeout_s=0.2` 时，卡 1 秒的调用必须被 0.2 秒的等待切断。"""
    adapter = _adapter(tmp_path)

    def slow_cli(args=None, prog_name=None, standalone_mode=None):  # noqa: ARG001
        time.sleep(1.0)

    monkeypatch.setattr(B, "_cli_entry", lambda: slow_cli)

    started = time.monotonic()
    envelope = adapter._run_cli("search")
    elapsed = time.monotonic() - started

    assert envelope["ok"] is False
    assert adapter.last_error is not None
    assert adapter.last_error["error_code"] == "TIMEOUT"
    assert elapsed < 0.9, f"timeout_s=0.2 却等了 {elapsed:.2f}s，超时没生效"
    assert B.cli_stuck() is True


def test_after_timeout_no_further_requests_are_sent(tmp_path, monkeypatch):
    """超时之后不许再发起上游调用（fail-closed），否则"卡死"会被伪装成"慢"。"""
    adapter = _adapter(tmp_path)
    calls: list[object] = []

    def slow_cli(args=None, **kwargs):  # noqa: ARG001
        calls.append(args)
        time.sleep(1.0)

    monkeypatch.setattr(B, "_cli_entry", lambda: slow_cli)

    first = adapter._run_cli("search")
    second = adapter._run_cli("search")

    assert first["error"]["code"] == "TIMEOUT"
    assert second["error"]["code"] == "TIMEOUT"
    assert len(calls) == 1, "超时后不许再向上游发请求"


def test_timeout_is_treated_as_unconfirmed_for_write_ops():
    """写操作超时 = "可能发出去了"，必须归 UNCONFIRMED（永不重试），不能记 failed。"""
    assert B.normalize_error_code("TIMEOUT") == "UNCONFIRMED"


def test_stuck_flag_is_time_limited_not_permanent(tmp_path, monkeypatch):
    """【2026-11-06 事故】暂停调用必须**到期自动恢复**。

    原实现是永久标记：09:19 一次瞬时卡顿（300 秒）把进程锁死，而实际上游几分钟
    就恢复了 —— 剩下 8 个半小时全在"立即失败 + 冷却 300 秒"里空转，当天只发出 1 条。
    这里把暂停窗口压成 0 秒，验证第二次会真的再调一次上游。
    """
    monkeypatch.setattr(B, "_STUCK_RETRY_SECONDS", 0)
    adapter = _adapter(tmp_path)
    calls: list[object] = []

    def slow_cli(args=None, **kwargs):  # noqa: ARG001
        calls.append(args)
        time.sleep(1.0)

    monkeypatch.setattr(B, "_cli_entry", lambda: slow_cli)

    first = adapter._run_cli("search")
    assert first["error"]["code"] == "TIMEOUT"
    assert B.cli_stuck() is False, "暂停窗口为 0 时应立即到期，而不是永久锁死"

    time.sleep(1.1)  # 等上一轮"卡住"的线程退出并释放 CLI 锁
    adapter._run_cli("search")
    assert len(calls) == 2, "暂停到期后必须真的再调用一次上游"


def test_normal_path_still_parses_envelope(tmp_path, monkeypatch):
    """加了线程与超时之后，正常信封的解析不许改变（回归）。"""
    adapter = _adapter(tmp_path)

    def ok_cli(args=None, prog_name=None, standalone_mode=None, **kwargs):  # noqa: ARG001
        print('{"ok": true, "data": [{"job_id": "j1"}], "error": null}')

    monkeypatch.setattr(B, "_cli_entry", lambda: ok_cli)

    envelope = adapter._run_cli("search")
    assert envelope["ok"] is True
    assert envelope["data"] == [{"job_id": "j1"}]
    assert B.cli_stuck() is False


def test_exception_inside_cli_is_still_upstream_error(tmp_path, monkeypatch):
    """线程里抛异常 → 仍然是 `UPSTREAM_ERROR`（不是 TIMEOUT、不许逃出 `_run_cli`）。"""
    adapter = _adapter(tmp_path)

    def bad_cli(args=None, **kwargs):  # noqa: ARG001
        raise RuntimeError("click 装配失败")

    monkeypatch.setattr(B, "_cli_entry", lambda: bad_cli)

    envelope = adapter._run_cli("search")
    assert envelope["ok"] is False
    assert adapter.last_error["error_code"] == "UPSTREAM_ERROR"
    assert B.cli_stuck() is False
