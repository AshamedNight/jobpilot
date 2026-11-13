"""环境体检里"能确定性判断"的部分。

体检本身是给人看的信息，但锁的判断必须准 —— 这是决定"今天能不能跑"的唯一依据。
"""

from __future__ import annotations

import json

from app.tools.envcheck import lock_state


def test_no_lock(tmp_path):
    assert "无锁" in lock_state(tmp_path)


def test_lock_with_reason(tmp_path):
    (tmp_path / "cdp_risk_lock.json").write_text(
        json.dumps({"reason": "ENVIRONMENT_RISK"}), encoding="utf-8"
    )
    text = lock_state(tmp_path)
    assert "有锁" in text
    assert "ENVIRONMENT_RISK" in text


def test_broken_lock_file_still_reports_lock(tmp_path):
    """锁文件写坏了也必须报"有锁"：宁可误报有锁，不可误报无锁。"""
    (tmp_path / "cdp_risk_lock.json").write_text("{坏", encoding="utf-8")
    assert "有锁" in lock_state(tmp_path)
