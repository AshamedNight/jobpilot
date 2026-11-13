"""前端冒烟：页面加载时**不允许有 JS 报错**（闭环 6）。

## 为什么必须有这条

2026-11-12 真实事故：抽 `notify.js` 时顺手删掉了 `let statusSnapshot = null;`，
`checkNotifications()` 每次刷新都抛 `ReferenceError`。
页面**照常渲染**（KPI 正常、表格正常），只有浏览器控制台里能看到那一行红字。

`node --check` 查不出来（语法没毛病），pytest 也查不出来（后端无关）。
唯一的办法是**真的用浏览器加载一次，并把 console 抓回来**。

Chrome 的 `--enable-logging=stderr --v=1` 会把渲染进程的 console 以
`ERROR:CONSOLE(...)` 打到 stderr —— 这就是我们的探针。

没有 Chrome 就跳过，不阻塞别的测试。
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

CHROME_CANDIDATES = (
    Path(r"C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path(r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
)
ROOT = Path(__file__).resolve().parents[1]

#: 这些字符出现在 ERROR:CONSOLE 里就判定为"前端真有错"
FATAL_MARKERS = ("Uncaught", "ReferenceError", "TypeError", "SyntaxError", "is not defined")


def _chrome() -> Path | None:
    for path in CHROME_CANDIDATES:
        if path.exists():
            return path
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def live_server(tmp_path):
    """在本进程内起一个真实的 uvicorn（随机端口 + 临时库），供 Chrome 访问。"""
    uvicorn = pytest.importorskip("uvicorn")
    from app.main import create_app

    app = create_app(db_path=tmp_path / "ui-smoke.db")
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while time.time() < deadline and not server.started:
        time.sleep(0.1)
    if not server.started:
        pytest.skip("本地服务没起来，跳过前端冒烟")

    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_page_has_no_javascript_errors(live_server, tmp_path):
    chrome = _chrome()
    if not chrome:
        pytest.skip("没有 Chrome，跳过前端冒烟")

    profile = tmp_path / "chrome-profile"
    proc = subprocess.run(  # noqa: S603
        [
            str(chrome),
            "--headless=new",
            "--disable-gpu",
            "--enable-logging=stderr",
            "--v=1",
            "--virtual-time-budget=7000",
            f"--user-data-dir={profile}",
            "--dump-dom",
            f"{live_server}/",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )

    log = (proc.stderr or "") + (proc.stdout or "")
    # 【坑】Chrome 把页面 console 一律打成 `INFO:CONSOLE`，**不管严重程度** ——
    # 只过滤 `ERROR:CONSOLE` 的话永远是空列表，测试就成了摆设（第一版就是这么写的，
    # 注入 bug 验证时才发现它照样"通过"）。所以先按 CONSOLE 抓，再看内容里的关键字。
    errors = [
        line
        for line in log.splitlines()
        if "CONSOLE" in line and any(marker in line for marker in FATAL_MARKERS)
    ]
    assert not errors, "页面加载时出现 JS 报错：\n" + "\n".join(errors[:5])
