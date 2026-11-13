"""把前端通知逻辑纳入 pytest（闭环 6）。

`diffStatus` 是纯函数，但它原来埋在 app.js（依赖 DOM）里，node 加载不了 —— 等于没测。
现在抽到 app/web/notify.js，用 node 直接跑单测；node 不在就跳过，不阻塞 CI。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "js" / "notify.test.mjs"
MANAGED_NODE = Path.home() / ".workbuddy" / "binaries" / "node" / "versions" / "22.22.2" / "node.exe"


def _node() -> str | None:
    if MANAGED_NODE.exists():
        return str(MANAGED_NODE)
    return shutil.which("node")


def test_notify_logic_passes_in_node():
    node = _node()
    if not node:
        pytest.skip("没有 node，跳过前端单测")
    proc = subprocess.run(  # noqa: S603
        [node, str(SCRIPT)], capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    assert "passed" in (proc.stdout or "")
