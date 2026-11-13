"""改 CDP 端口必须两处同步（2026-11-13）。

只改 `config.yaml` 不改上游 `config.json` 会**静默失效**：
CLI 还连老端口，表现为 `BROWSER_SESSION_NOT_FOUND` 或超时，
看起来像"浏览器没登录"，极难猜。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.tools import rebind_cdp


def test_update_yaml_only_touches_target_account(tmp_path, monkeypatch):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "accounts:/n"
        "  - id: b\n"
        "    name: B\n"
        "    cdp_port: 9222\n"
        "  - id: a\n"
        "    name: A\n"
        "    cdp_port: 9223\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rebind_cdp, "CONFIG_YAML", yaml_path)

    assert rebind_cdp.update_yaml("b", 9333) is True
    text = yaml_path.read_text(encoding="utf-8")
    assert "cdp_port: 9333" in text
    assert "cdp_port: 9223" in text, "另一个账号不许被改"


def test_update_upstream_rewrites_cdp_url(tmp_path):
    data_dir = tmp_path / "agent"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(
        json.dumps({"cdp_url": "http://127.0.0.1:9222", "browser_source": "existing-browser"}),
        encoding="utf-8",
    )
    assert rebind_cdp.update_upstream(data_dir, 9333) is True
    payload = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
    assert payload["cdp_url"] == "http://127.0.0.1:9333"
    assert payload["browser_source"] == "existing-browser", "其余字段不许动"


def test_update_upstream_missing_file_reports_false(tmp_path):
    assert rebind_cdp.update_upstream(tmp_path / "nope", 9333) is False


def test_chrome_command_contains_new_port(tmp_path):
    account = {
        "chrome_exe": "chrome.exe",
        "chrome_profile": str(tmp_path / "prof"),
        "cdp_port": 9222,
    }
    cmd = rebind_cdp.chrome_command(account, 9333)
    assert "--remote-debugging-port=9333" in cmd
    assert "--user-data-dir=" in cmd
