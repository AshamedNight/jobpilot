"""测试环境隔离回归 —— 2 条（P2-B F1，2026-10-27 事故）。

事故现场：用户 Win11 真机 shell 里留着 `$env:ADAPTER="boss"`，`pytest -q` 变成
**7 failed / 84 passed**；`Remove-Item Env:\\ADAPTER` 之后立刻 91 passed。
交付代码一行没坏，坏的是"测试套件对环境不免疫"。

这两条守的是 `tests/conftest.py::_hermetic_environment` 的**两半**：

1. 环境变量那半：shell 里不许再有 `ADAPTER` / `JOBPILOT_FAKE_SCRIPT`；
2. config 那半：**仓库 yaml 被改成 `adapter: boss` 也压得回来**。

第 2 条是本批的命门——它是唯一能证明"守卫真的在起作用"的测试：
把 conftest 里的 `autouse=True` 摘掉，本文件 2 条 + 原有 7 条 e2e 会一起变红。
"""

from __future__ import annotations

import os
from pathlib import Path

from app.adapters.fake import FakeAdapter
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_hermetic_strips_ambient_adapter_env():
    """autouse fixture 必须把 shell 里的适配器开关清掉（层 1）。"""
    assert "ADAPTER" not in os.environ
    assert "JOBPILOT_FAKE_SCRIPT" not in os.environ


def test_boss_configured_repo_yaml_cannot_hijack_the_suite(tmp_path: Path):
    """仓库 yaml 被改成 `adapter: boss` 也不许劫持测试（层 2）。

    用**临时目录里的一份 boss 配置**测，不动仓库 `config.yaml`
    （改真文件测隔离，等于用脏手抓干净东西）。
    """
    boss_cfg = tmp_path / "config.yaml"
    boss_cfg.write_text(
        (REPO_ROOT / "config.yaml").read_text(encoding="utf-8").replace(
            "adapter: fake", "adapter: boss"
        ),
        encoding="utf-8",
    )
    app = create_app(
        config_path=boss_cfg,
        db_path=tmp_path / "h.db",
        env_path=tmp_path / "no.env",
    )
    # 配置里写着 boss，测试里必须仍然是 FakeAdapter（否则 7 条 e2e 会静默失真）
    assert isinstance(app.state.adapter, FakeAdapter)
