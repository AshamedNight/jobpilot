"""pytest 公共 fixture。

注意：所有 fixture **自建数据**，不拿仓库里的配置文件当被测输入（N7：
用仓库文件测归一会让"配置本身合法"和"代码能处理非法值"互相污染）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import models  # noqa: E402  （必须先改 sys.path）


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """临时库文件（用文件而不是 :memory:，这样 WAL 和多线程才是真的被测到）。"""
    return tmp_path / "jobpilot-test.db"


@pytest.fixture()
def conn(db_path: Path):
    """建好六张表的连接，用完关掉。"""
    connection = models.init_db(db_path)
    try:
        yield connection
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# 【P2-B F1 · 测试环境隔离】2026-10-27 事故：真机 shell 里 `$env:ADAPTER="boss"`
# 留着（或 `setx` 成用户级变量），整个 pytest 就变成 boss 适配器，7 条 e2e 立刻失真
# （`found=0` / `greeted=0` / `isinstance(BossAdapter, FakeAdapter)` 为假）。
#
# 两层，互相独立，任一层单独都能打红：
#   层 1  `app/main.py` 的 `os.environ.get("ADAPTER")` 优先级高于 config.yaml
#         —— 这是 P2-A 补丁 §1.3 加的接线，**必须保留**（真机要用），所以只能在测试侧清；
#   层 2  7 处 `create_app()` 没传 `config_path`，读的是仓库 `config.yaml`，
#         而真机流程要求反复把它改成 `adapter: boss` / 改 quota。
#
# 修法：autouse 把"外部 shell 的一切"和"仓库 yaml 里的 adapter"都压回 fake。
# **不碰生产代码**（不许加 `if TESTING` 分支），也不新增测试专用 config 文件
# （多一份就会漂移，压一个字段就够）。
# --------------------------------------------------------------------------- #
_AMBIENT_ENV = ("ADAPTER", "JOBPILOT_FAKE_SCRIPT", "JOBPILOT_DB", "LLM_API_KEY")

#: 【F1-b · 2026-10-27 第二次事故】测试的**额度基线**。
#:
#: 只压 `adapter` 是不够的：B 组真机验收要求反复改同一个 `config.yaml` 的
#: `quota.daily_greet`（5→0→3）与 `quota.daily_detail`（50→已用量→30），
#: 一旦有值留在那里，所有"假设能发 4 条 / 能抓 N 条"的断言立刻失真
#: （实测 `adapter: boss` + `daily_greet: 0` → 9 failed，其中 5 条 e2e + 2 条 sweep
#:   全是 `assert 0 == 4` 这一族）。
#:
#: 这些值就是仓库 `config.yaml` 的出厂值（`daily_greet: 5` / `daily_greet_max: 50`
#: / `daily_detail: 50` / `detail_batch: 10`），写在这里是为了让"测试基线"显式、
#: 可见、可改——而不是隐式依赖某个文件的当前内容。
TEST_QUOTA_BASELINE: dict[str, int] = {
    "daily_greet": 5,
    "daily_greet_max": 50,
    "daily_detail": 50,
    "detail_batch": 10,
}


def force_fake_adapter(cfg: dict) -> dict:
    """把 `cfg` 的 adapter 压成 fake（真机流程会把 `config.yaml` 改成 boss）。"""
    cfg["adapter"] = "fake"
    return cfg


def force_test_baseline(cfg: dict) -> dict:
    """`adapter` + `quota` 一起压到测试基线。

    **只作用于 `load_config()` 这一层**：测试在 `create_app()` **之后**自己写的额度
    （`client.app.state.config["quota"]["daily_greet"] = 2`、`detail_batch = 2`）
    仍然生效——那是测试的显式意图，压基线不该覆盖它。

    `test_config.py` 里"故意写坏值、期望 `StartupError`"的用例也不受影响：
    异常在 `validate_quota()` 阶段就抛了，比这里早。

    【闭环 1 补充】`pacing.ramp` 也必须压到 `daily_greet` 以内：仓库里的 ramp 是
    真机用的 `[15, 25, 40, 50]`，而测试基线 `daily_greet=5`，不压就会得到
    "ramp 超日额度"的启动失败——那是配置口径不一致的假红，不是被测代码的问题。
    """
    cfg = force_fake_adapter(cfg)
    quota = dict(cfg.get("quota") or {})
    quota.update(TEST_QUOTA_BASELINE)
    cfg["quota"] = quota

    pacing = dict(cfg.get("pacing") or {})
    if pacing.get("enabled"):
        pacing["ramp"] = [int(quota["daily_greet"])]
        # 测试不跑真实等待：轮间长休压成 0，间隔仍是真机值（间隔下限 60s 有硬校验，
        # 想压也压不下来，而 pacer 的单测只调 `_tick()`，不真的 sleep）
        pacing["round_gap_s"] = [0, 0]
    cfg["pacing"] = pacing
    return cfg


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch):
    """【F1】全局自动生效：测试行为只由测试自己决定，不由 shell / 仓库 yaml 决定。"""
    # ① 环境变量：真机 shell 里留下的东西一个都不许影响断言
    for name in _AMBIENT_ENV:
        monkeypatch.delenv(name, raising=False)

    # ② 仓库 config.yaml 里的 `adapter: boss` 在测试里压回 fake
    #    （只包 `load_config` 这一层，适配器选择逻辑本身不动）
    import app.main as main_module

    real_load_config = main_module.load_config
    monkeypatch.setattr(
        main_module,
        "load_config",
        lambda *a, **kw: force_test_baseline(real_load_config(*a, **kw)),
    )

@pytest.fixture(autouse=True)
def _isolate_probe_budget(tmp_path, monkeypatch):
    """把「每日探测配额」隔离到临时文件。

    否则跑几次测试就把真实的"今天还能探测几次"用光（2026-11-13 真实发生：
    `test_recover_tool` 连跑三次 recover 直接触顶，把一个用例判成失败）。
    """
    monkeypatch.setenv("JOBPILOT_PROBE_BUDGET", str(tmp_path / "probe_budget.json"))
