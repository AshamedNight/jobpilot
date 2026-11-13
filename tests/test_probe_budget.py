"""每日探测配额（2026-11-13）。

撞过的 5 次环境风控里至少 3 次是"诊断性请求"自己敲出来的。人的自控力靠不住，
所以做成会失败的硬限制：超了直接拒绝执行，要突破必须显式 --force。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.core import probe_budget


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    # 用环境变量隔离（生产代码优先读它），与本文件其它用例互不影响
    monkeypatch.setenv(probe_budget.ENV_OVERRIDE, str(tmp_path / "probe_budget.json"))


def test_consume_until_limit():
    for i in range(3):
        allowed, state = probe_budget.consume("recover_probe", limit=3)
        assert allowed is True
        assert state["count"] == i + 1
        assert state["remaining"] == 2 - i

    allowed, state = probe_budget.consume("recover_probe", limit=3)
    assert allowed is False
    assert state["count"] == 3, "被拒不计数：拒绝本身不产生请求"
    assert state["remaining"] == 0


def test_new_day_resets(tmp_path, monkeypatch):
    probe_budget.consume("recover_probe", limit=1)
    Path(os.environ[probe_budget.ENV_OVERRIDE]).write_text(
        json.dumps({"day": "2000-01-01", "count": 99, "events": []}), encoding="utf-8"
    )
    allowed, state = probe_budget.consume("recover_probe", limit=1)
    assert allowed is True, "跨天必须重置，否则昨天试过的次数会卡死今天"
    assert state["count"] == 1


def test_broken_file_does_not_crash():
    Path(os.environ[probe_budget.ENV_OVERRIDE]).write_text("{坏", encoding="utf-8")
    allowed, state = probe_budget.consume("recover_probe", limit=2)
    assert allowed is True
    assert state["count"] == 1


def test_events_are_recorded():
    probe_budget.consume("greet_probe", limit=3)
    state = probe_budget.status(limit=3)
    assert state["events"][0]["kind"] == "greet_probe"
    assert "at" in state["events"][0]
