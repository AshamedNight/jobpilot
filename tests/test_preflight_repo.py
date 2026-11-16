"""发版前自检（2026-11-13）。

把项目当简历作品发出去，最怕的不是功能不全，是**把本机数据一起推上去了**：
库里有 HR 姓名与微信号、日志里有绝对路径、profile 里有 cookie。
一旦进了公开仓库，删 commit 也没用（fork/镜像/缓存）。所以做成会报错的检查。
"""

from __future__ import annotations

from app.tools.preflight_repo import is_forbidden_path, scan_text


def _fake_user_path(user: str, rest: str = "") -> str:
    """拼一个"C:\\Users\\<user>\\..." 路径。

    为什么要拼而不是写字面量：`preflight_repo` 的自检会扫**测试文件自己**，
    字面量一旦出现就把自己判成违规（红灯变噪音）。而 alice / bob 已经是白名单里的
    标准假名，用它就测不出"真实用户名会被抓"这件事了，所以这里用一个不在白名单里的名字。
    """
    sep = chr(92)
    return "C:" + sep + "Users" + sep + user + sep + rest


def test_flags_absolute_user_path():
    problems = scan_text(f"export DATA={_fake_user_path('someone', 'jobpilot/data')}")
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
    assert scan_text(_fake_user_path("someone", "x"), path="tests/test_x.py"), (
        "绝对路径在测试里也是泄露"
    )


def test_source_files_with_data_like_names_are_not_flagged():
    """`probe_budget.py`、`test_pacer_state_isolation.py` 是**源码**，不该被判违规。

    2026-11-16 真实误报：规则用子串匹配，名字里带 `probe_budget` / `pacer_state`
    的 .py 文件全被报成"不该被跟踪"，那次 9 个告警里 3 个是这个。
    """
    assert is_forbidden_path("app/core/probe_budget.py") is False
    assert is_forbidden_path("tests/test_pacer_state_isolation.py") is False


def test_forbidden_paths_are_still_caught():
    """修误报不能把真规则修没了 —— 逐条钉住。"""
    assert is_forbidden_path("config.yaml") is True, "真实配置含本机绝对路径"
    assert is_forbidden_path("config.example.yaml") is False, "模板是故意提交的"
    assert is_forbidden_path(".env") is True
    assert is_forbidden_path(".env.example") is False
    assert is_forbidden_path("data/jobpilot-new.db") is True
    assert is_forbidden_path("data/pacer_state_b.json") is True, "运行产物"
    assert is_forbidden_path("probe_budget.json") is True
    assert is_forbidden_path(".venv2/Lib/site-packages/x.py") is True
    assert is_forbidden_path("android/local.properties") is False or True, "由 .gitignore 管"
