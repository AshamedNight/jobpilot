"""闭环 3：多账号注册表与隔离。

这里钉住四件事：
1. 缺字段 / id 重名 / 端口冲突 → 启动期就拒绝（宁可起不来，不可发错号）
2. 指针文件的读写（损坏/缺失都不许抛）
3. `env_for` 把账号翻译成子进程环境（库、上游目录、profile）
4. `apply_override` 只改资源路径，**不改额度/节奏/目标**
"""

from __future__ import annotations

import json

import pytest

from app.core import accounts


def _cfg(*entries) -> dict:
    return {"accounts": list(entries), "adapter_options": {"data_dir": "C:/default"}}


def _entry(**overrides):
    base = {
        "id": "x",
        "name": "X 号",
        "db": "data/x.db",
        "data_dir": "C:/agent-x",
        "chrome_profile": "C:/profile-x",
        "cdp_port": 9222,
    }
    base.update(overrides)
    return base


def test_registry_requires_all_fields():
    cfg = _cfg({"id": "x", "name": "X"})
    with pytest.raises(accounts.AccountError) as exc:
        accounts.registry(cfg)
    assert "缺少字段" in str(exc.value)


def test_registry_rejects_duplicate_id():
    with pytest.raises(accounts.AccountError, match="重复"):
        accounts.registry(_cfg(_entry(id="x", service_port=8010), _entry(id="x", cdp_port=9223, service_port=8020)))


def test_registry_rejects_port_conflict():
    """同一 profile 不能开两个实例；端口撞了就是"两个账号抢同一个浏览器"。"""
    with pytest.raises(accounts.AccountError, match="调试端口"):
        accounts.registry(_cfg(_entry(id="x", service_port=8010), _entry(id="y", service_port=8020)))


def test_pointer_roundtrip_and_corruption(tmp_path, monkeypatch):
    pointer = tmp_path / "active_account.json"
    monkeypatch.setattr(accounts, "POINTER_PATH", pointer)

    assert accounts.read_pointer() is None, "文件不存在 → None，不抛"

    accounts.write_pointer("b")
    assert accounts.read_pointer() == "b"

    pointer.write_text("{oops", encoding="utf-8")
    assert accounts.read_pointer() is None, "损坏的指针也要容错"


def test_active_account_prefers_env(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "POINTER_PATH", tmp_path / "p.json")
    accounts.write_pointer("b")
    cfg = _cfg(_entry(id="a", cdp_port=9223, service_port=8020), _entry(id="b", service_port=8010))

    monkeypatch.delenv("JOBPILOT_ACCOUNT", raising=False)
    assert accounts.active_account(cfg)["id"] == "b", "没有环境变量时看指针"

    monkeypatch.setenv("JOBPILOT_ACCOUNT", "a")
    assert accounts.active_account(cfg)["id"] == "a", "环境变量优先（已启动的进程要稳定）"


def test_env_for_maps_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "POINTER_PATH", tmp_path / "p.json")
    monkeypatch.delenv("JOBPILOT_ACCOUNT", raising=False)
    env = accounts.env_for(_entry(id="a", db="data/jobpilot-a.db"))

    assert env["JOBPILOT_ACCOUNT"] == "a"
    assert env["JOBPILOT_DB"].endswith("jobpilot-a.db")
    assert env["JOBPILOT_BOSS_CRED_DIR"] == "C:/agent-x"
    assert env["JOBPILOT_CHROME_PROFILE_DIR"] == "C:/profile-x"


def test_apply_override_only_touches_resources(tmp_path, monkeypatch):
    """额度/节奏不该跟着账号漂移 —— 那是业务策略，必须由人决定。"""
    monkeypatch.setattr(accounts, "POINTER_PATH", tmp_path / "p.json")
    monkeypatch.setenv("JOBPILOT_ACCOUNT", "a")
    cfg = _cfg(_entry(id="a", data_dir="C:/agent-a"))
    cfg["quota"] = {"daily_greet": 15}
    cfg["pacing"] = {"ramp": [15, 25]}

    out = accounts.apply_override(cfg)

    assert out["adapter_options"]["data_dir"] == "C:/agent-a"
    assert out["quota"] == {"daily_greet": 15}, "额度不许被账号覆盖"
    assert out["pacing"] == {"ramp": [15, 25]}, "节奏不许被账号覆盖"
    assert out["active_account"]["id"] == "a"


def test_no_active_account_keeps_config_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "POINTER_PATH", tmp_path / "p.json")
    monkeypatch.delenv("JOBPILOT_ACCOUNT", raising=False)
    cfg = _cfg(_entry(id="a"))
    out = accounts.apply_override(cfg)
    assert out["adapter_options"]["data_dir"] == "C:/default"
    assert "active_account" not in out


def test_real_config_registry_is_valid():
    from app.config import load_config

    items = accounts.registry(load_config())
    assert items, "真实配置至少要有一个账号"
    for item in items:
        assert item["cdp_port"] > 0
        assert not str(item["data_dir"]).count("//"), "路径里出现重复斜杠说明配置写坏了"


def test_service_port_must_be_unique(tmp_path, monkeypatch):
    """并行时每个账号要有自己的控制台端口，撞了就是两个 pacer 抢一个端口。"""
    monkeypatch.setattr(accounts, "POINTER_PATH", tmp_path / "p.json")
    cfg = _cfg(
        _entry(id="x", cdp_port=9222, service_port=8010),
        _entry(id="y", cdp_port=9223, service_port=8010),
    )
    with pytest.raises(accounts.AccountError, match="服务端口"):
        accounts.registry(cfg)


def test_service_port_defaults_to_8010(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "POINTER_PATH", tmp_path / "p.json")
    items = accounts.registry(_cfg(_entry(id="x")))
    assert items[0]["service_port"] == accounts.DEFAULT_SERVICE_PORT
    assert items[0]["enabled"] is True


def test_disabled_account_is_still_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "POINTER_PATH", tmp_path / "p.json")
    items = accounts.registry(_cfg(_entry(id="x", enabled=False)))
    assert items[0]["enabled"] is False


def test_account_can_override_pacing_and_quota(tmp_path, monkeypatch):
    """每号独立节奏/额度：错峰的前提（2026-11-12 用户明确要多号各 50 条/天）。"""
    monkeypatch.setattr(accounts, "POINTER_PATH", tmp_path / "p.json")
    monkeypatch.setenv("JOBPILOT_ACCOUNT", "a")
    cfg = _cfg(
        _entry(
            id="a",
            service_port=8020,
            pacing={"segments": [["10:00", "13:00"]], "interval_s": [120, 180]},
            quota={"daily_greet": 50},
        )
    )
    cfg["pacing"] = {"segments": [["09:00", "12:00"]], "round": [3, 5]}
    cfg["quota"] = {"daily_greet": 15, "daily_greet_max": 50}

    out = accounts.apply_override(cfg)

    assert out["pacing"]["segments"] == [["10:00", "13:00"]], "账号的时段整段覆盖"
    assert out["pacing"]["interval_s"] == [120, 180]
    assert out["pacing"]["round"] == [3, 5], "没覆盖的键仍然继承全局"
    assert out["quota"]["daily_greet"] == 50, "每号自己的日目标"
    assert out["quota"]["daily_greet_max"] == 50


def test_account_without_pacing_override_keeps_global(tmp_path, monkeypatch):
    """没配 pacing 的账号行为必须和以前**完全一致**（明天要跑，不能有任何漂移）。"""
    monkeypatch.setattr(accounts, "POINTER_PATH", tmp_path / "p.json")
    monkeypatch.setenv("JOBPILOT_ACCOUNT", "b")
    cfg = _cfg(_entry(id="b"))
    cfg["pacing"] = {"segments": [["09:00", "12:00"]], "round": [3, 5]}
    cfg["quota"] = {"daily_greet": 25}

    out = accounts.apply_override(cfg)

    assert out["pacing"] == {"segments": [["09:00", "12:00"]], "round": [3, 5]}
    assert out["quota"] == {"daily_greet": 25}
