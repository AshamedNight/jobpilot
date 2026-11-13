"""起跑结果必须如实呈现（2026-11-13 事故）。

接口在风控锁着时返回 `{"ok": false, "error": ...}`（**没有 status 字段**）。
原实现直接读 `resp["status"]`，打印出「已起跑: used=None target=None next=None」——
看起来成功、实际一条不发。人会以为今天在跑，半天后才发现 used 是 0。
"""

from __future__ import annotations

from app.tools.launch import format_start_result


def test_rejection_is_reported_as_rejection():
    text = format_start_result({"ok": False, "error": "search 命中风控（ENVIRONMENT_RISK_LOCKED）"})
    assert "被拒绝" in text
    assert "None" not in text, "绝不能出现 None 这种假字段"
    assert "recover" in text, "风控拒绝要给出下一步动作"


def test_success_reports_real_numbers():
    text = format_start_result(
        {"ok": True, "status": {"used": 3, "target": 25, "next_at": "2026-11-13T10:00:00", "next_reason": "interval"}}
    )
    assert "used=3" in text and "target=25" in text
    assert "被拒绝" not in text


def test_missing_status_does_not_print_none():
    text = format_start_result({"ok": True})
    assert "None" not in text
    assert "已受理" in text
