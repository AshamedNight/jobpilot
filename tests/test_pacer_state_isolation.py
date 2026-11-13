"""多账号并行的最隐蔽串号：pacer 的熔断/冷却状态文件。

默认路径是 `db_path.parent / "pacer_state.json"`。多个账号的 db 都放 `data/` 下时，
它们会**共用同一个 state 文件** —— A 号熔断 24 小时，B 号跟着一起躺。
2026-11-12 用户明确要"每个账号各自 50 条/天"，这一条必须先钉住。
"""

from __future__ import annotations

from app.main import create_app


def test_pacer_state_is_per_account(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBPILOT_ACCOUNT", "b")
    app = create_app(db_path=tmp_path / "jobpilot-new.db")
    state_path = str(app.state.pacer._state_path)
    assert state_path.endswith("pacer_state_b.json"), state_path


def test_pacer_state_keeps_legacy_name_without_account(tmp_path, monkeypatch):
    """没有账号（测试/单实例默认）时保持老路径，避免把既有熔断状态孤儿化。"""
    monkeypatch.delenv("JOBPILOT_ACCOUNT", raising=False)
    app = create_app(db_path=tmp_path / "jobpilot.db")
    assert str(app.state.pacer._state_path).endswith("pacer_state.json")


def test_two_accounts_do_not_share_state(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBPILOT_ACCOUNT", "a")
    app_a = create_app(db_path=tmp_path / "jobpilot-a.db")
    monkeypatch.setenv("JOBPILOT_ACCOUNT", "b")
    app_b = create_app(db_path=tmp_path / "jobpilot-b.db")

    assert app_a.state.pacer._state_path != app_b.state.pacer._state_path
