"""发版前自检（2026-11-13）。

把项目当简历作品发出去，最怕的不是功能不全，是**把本机数据一起推上去了**：
库里有 HR 姓名与微信号、日志里有绝对路径、profile 里有 cookie。
一旦进了公开仓库，删 commit 也没用（fork/镜像/缓存）。所以做成会报错的检查。
"""

from __future__ import annotations

from app.tools.preflight_repo import scan_text


def test_flags_absolute_user_path():
    # 用正斜杠写法，避免测试文件本身被 Windows 转义规则咬到
    problems = scan_text("export DATA=C:/Users/alice/jobpilot/data")
    assert problems and "绝对路径" in problems[0]


def test_allows_placeholder_path():
    assert scan_text('data_dir: "C:/Users/<you>/.boss-agent-jobpilot"') == []


def test_flags_real_looking_wechat_line():
    # 用明显编造的号：既要能命中"微信号"规则，又不能把真实号码写进仓库
    problems = scan_text("王女士的微信号：demo_wx_888")
    assert any("微信号" in item for item in problems), problems


def test_allows_known_fake_samples():
    assert scan_text("号码 13800138000 不该被当成泄露") == []


def test_tests_dir_is_treated_as_fixtures():
    """测试夹具里的手机号/微信号是编的，不算泄露；但绝对路径仍然要报。"""
    assert scan_text("联系 13900001111", path="tests/test_x.py") == []
    assert scan_text("C:/Users/bob/x", path="tests/test_x.py"), "绝对路径在测试里也是泄露"
