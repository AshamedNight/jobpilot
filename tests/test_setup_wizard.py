"""首次启动向导的确定性部分（2026-11-13）。

"下载即用"的门槛不在功能，在**改三处 YAML 路径**。向导把这一步搬到页面上，
但写文件这件事必须可测：写错了，新用户拿到的是一个跑不起来的配置。
"""

from __future__ import annotations

import pathlib

import pytest

from app.core import setup as setup_mod


@pytest.fixture
def temp_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    example = tmp_path / "config.example.yaml"
    example.write_text(
        "adapter: boss\n"
        "adapter_options:\n"
        "  data_dir: 'C:/Users/<you>/.boss-agent-jobpilot'\n"
        "accounts:\n"
        "  - id: b\n"
        '    name: "主账号"\n'
        "    chrome_exe: \"chrome.exe\"\n"
        '    chrome_profile: "C:/Users/<you>/chrome-jobpilot"\n'
        '    data_dir: "C:/Users/<you>/.boss-agent-jobpilot"\n'
        "    cdp_port: 9333\n"
        "pacing:\n"
        "  send_batch: 4\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_mod, "CONFIG_YAML", cfg)
    monkeypatch.setattr(setup_mod, "EXAMPLE_YAML", example)
    return cfg


def test_placeholder_detection():
    assert setup_mod.placeholder_like("<you>") is True
    assert setup_mod.placeholder_like("") is True
    assert setup_mod.placeholder_like("C:/Users/alice/chrome") is False


def test_status_says_needed_when_placeholder(temp_config, tmp_path):
    # 先让配置文件存在（内容还是占位符），才测得到"字段没填"这条
    temp_config.write_text(setup_mod.EXAMPLE_YAML.read_text(encoding="utf-8"), encoding="utf-8")
    info = setup_mod.status()
    assert info["needed"] is True
    assert any("没填" in item for item in info["missing"])


def test_apply_creates_config_and_writes_paths(temp_config, tmp_path):
    chrome = tmp_path / "chrome.exe"
    chrome.write_text("", encoding="utf-8")
    profile = tmp_path / "profile"
    data = tmp_path / "data"

    result = setup_mod.apply(str(chrome), str(profile), str(data), cdp_port=9444)

    assert result["ok"] is True, result
    text = temp_config.read_text(encoding="utf-8")
    assert str(profile).replace("\\", "/") in text
    assert str(data).replace("\\", "/") in text
    assert "cdp_port: 9444" in text
    assert profile.exists() and data.exists(), "目录要被创建出来"


def test_apply_keeps_other_sections_untouched(temp_config, tmp_path):
    """向导只配路径，**不许改** pacing/quota/target —— 那是业务策略。"""
    chrome = tmp_path / "chrome.exe"
    chrome.write_text("", encoding="utf-8")
    setup_mod.apply(str(chrome), str(tmp_path / "p"), str(tmp_path / "d"))
    text = temp_config.read_text(encoding="utf-8")
    assert "send_batch: 4" in text


def test_apply_rejects_missing_chrome(temp_config):
    result = setup_mod.apply("C:/nope/chrome.exe", "C:/p", "C:/d")
    assert result["ok"] is False
    assert "不存在" in result["error"]


def test_apply_rejects_empty_dirs(temp_config, tmp_path):
    chrome = tmp_path / "chrome.exe"
    chrome.write_text("", encoding="utf-8")
    assert setup_mod.apply(str(chrome), "", str(tmp_path / "d"))["ok"] is False
    assert setup_mod.apply(str(chrome), str(tmp_path / "p"), "")["ok"] is False
