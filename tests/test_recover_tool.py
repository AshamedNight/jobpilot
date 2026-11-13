"""风控恢复脚本的测试（`app.tools.recover`）。

这个脚本会删锁、改文件名，所以测试的重点不是"功能多强"，而是**它不会乱来**：

1. 没加 `--confirmed` 时一个文件都不许动（防"顺手跑一下就清了"）
2. 探测没通过时**不许**归档熔断（否则第二天一启动就撞锁，人还以为恢复好了）
3. 归档是改名不是删除（证据要留）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tools import recover


def _cfg(data_dir: Path) -> dict:
    return {
        "adapter": "boss",
        "adapter_options": {"data_dir": str(data_dir)},
        "target": {"cities": ["淄博"]},
    }


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data_dir = tmp_path / "cred"
    data_dir.mkdir()
    db = tmp_path / "data" / "jobpilot-new.db"
    db.parent.mkdir()
    db.touch()

    monkeypatch.setattr("app.config.load_config", lambda *a, **kw: _cfg(data_dir))
    return {"data_dir": data_dir, "db": db, "tmp": tmp_path}


def test_without_confirm_touches_nothing(env, capsys):
    lock = env["data_dir"] / "cdp_risk_lock.json"
    lock.write_text("{}", encoding="utf-8")
    state = env["db"].parent / "pacer_state.json"
    state.write_text('{"halted_until": "2026-11-05T20:00:00"}', encoding="utf-8")

    code = recover.main(["--db", str(env["db"])])

    assert code == 4, "没确认时应当拒绝，并以 4 退出"
    assert lock.exists(), "没确认就删锁是最危险的行为"
    assert state.exists(), "熔断文件不许被动到"
    out = capsys.readouterr().out
    assert "只做了体检" in out


def test_clear_locks_removes_both_and_dry_run_keeps(env):
    a = env["data_dir"] / "cdp_risk_lock.json"
    a.write_text("{}", encoding="utf-8")

    assert recover.clear_locks([a], dry_run=True) == [str(a)]
    assert a.exists(), "dry-run 不许真删"

    assert recover.clear_locks([a]) == [str(a)]
    assert not a.exists()


def test_archive_state_renames_and_keeps_bytes(env):
    state = env["db"].parent / "pacer_state.json"
    payload = '{"halted_until": "2026-11-05T20:00:00", "last_verdict": "risk"}'
    state.write_text(payload, encoding="utf-8")

    target = recover.archive_state(state)

    assert target is not None
    assert not state.exists(), "原文件要挪走（否则启动仍被熔断）"
    assert Path(target).read_text(encoding="utf-8") == payload, "归档必须是改名，不能丢证据"


def test_probe_failure_does_not_archive(env, monkeypatch, capsys):
    state = env["db"].parent / "pacer_state.json"
    state.write_text('{"halted_until": "2026-11-05T20:00:00"}', encoding="utf-8")
    monkeypatch.setattr(recover, "probe", lambda cfg, page: (False, "仍被风控拒绝（ENVIRONMENT_RISK）"))

    code = recover.main(["--confirmed", "--db", str(env["db"])])

    assert code == 2
    assert state.exists(), "探测没过就归档熔断 = 明天一启动就撞锁"
    assert "今天到此为止" in capsys.readouterr().out


def test_success_path_clears_probes_and_archives(env, monkeypatch, capsys):
    lock = env["data_dir"] / "cdp_risk_lock.json"
    lock.write_text("{}", encoding="utf-8")
    state = env["db"].parent / "pacer_state.json"
    state.write_text('{"halted_until": "2026-11-05T20:00:00"}', encoding="utf-8")
    monkeypatch.setattr(recover, "probe", lambda cfg, page: (True, "放行，单页返回 10 条岗位"))

    code = recover.main(["--confirmed", "--db", str(env["db"])])

    assert code == 0
    assert not lock.exists()
    assert not state.exists()
    assert list((env["db"].parent).glob("pacer_state.recovered-*.json.bak")), "归档文件应留在同目录"
    out = capsys.readouterr().out
    assert "inflight" in out, "结尾必须提醒看 inflight，而不是 last_verdict"


# --------------------------------------------------------------------------- #
# 浏览器拉起（2026-11-07：电脑重启后一个 Chrome 进程都没有）
# --------------------------------------------------------------------------- #
def test_launch_cache_round_trip(tmp_path):
    """上次用的 exe/profile 要能存下来 —— 电脑重启后这是唯一的线索。"""
    cache = tmp_path / "chrome_launch.json"
    assert recover._load_launch(cache) is None

    recover._save_launch(cache, r"C:\chrome.exe", r"C:\Users\x\chrome-boss", 9222)

    assert recover._load_launch(cache) == {
        "exe": r"C:\chrome.exe",
        "profile": r"C:\Users\x\chrome-boss",
        "port": 9222,
    }


def test_restart_refuses_without_any_hint(env, tmp_path, monkeypatch):
    """既没有进程、也没有缓存和环境变量时，必须**拒绝**而不是瞎猜路径。"""
    monkeypatch.delenv("JOBPILOT_CHROME_EXE", raising=False)
    monkeypatch.delenv("JOBPILOT_CHROME_PROFILE_DIR", raising=False)
    monkeypatch.setattr(recover, "_find_debug_chrome", lambda port: (None, "没找到"))

    ok, detail = recover.restart_browser(_cfg(env["data_dir"]), cache_path=tmp_path / "none.json")

    assert ok is False
    assert "手动起一次" in detail
