"""多账号起跑前的锁检查（2026-11-13）。

踩过的坑：先查 `/api/status` 判断"有没有锁"，但那个服务是恢复到一半之前启动的，
内存里留着 fail-closed 标记 —— 锁文件明明已经清掉，脚本仍说"锁还在"，
白白挡住一次正常起跑，人以为环境还没恢复。

结论：锁是**文件系统事实**，就去文件系统看。
"""

from __future__ import annotations

from app.tools.multi import _risk_locked


def test_no_lock_returns_false(tmp_path):
    (tmp_path / "data").mkdir()
    account = {"data_dir": str(tmp_path / "data"), "budget_dir": str(tmp_path / "budget")}
    assert _risk_locked(account) is False


def test_lock_in_data_dir_detected(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "cdp_risk_lock.json").write_text("{}", encoding="utf-8")
    assert _risk_locked({"data_dir": str(data_dir), "budget_dir": str(tmp_path / "b")}) is True


def test_lock_in_budget_dir_detected(tmp_path):
    budget = tmp_path / "b"
    budget.mkdir()
    (budget / "cdp_risk_lock.json").write_text("{}", encoding="utf-8")
    assert _risk_locked({"data_dir": str(tmp_path / "d"), "budget_dir": str(budget)}) is True


def test_missing_dirs_are_not_locks(tmp_path):
    assert _risk_locked({"data_dir": str(tmp_path / "nope"), "budget_dir": ""}) is False
