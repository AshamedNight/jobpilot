"""打包入口（PyInstaller 用）。

## 为什么单独写一个入口，而不是直接打 `app/main.py`

`app.main` 只负责 `create_app()`，本身不会启动服务器 —— 直接打成 exe 会"双击后
什么都没发生就退出"。而且我们的 `app/tools/launch.py` 是用
`subprocess.Popen([sys.executable, "-m", "uvicorn", ...])` 起服务的，
在冻结后的 exe 里 `sys.executable` 就是 exe 自己，会变成自己拉起自己。

所以这里做三件事，顺序固定：

1. `create_app()` 建应用
2. 1.5 秒后自动打开浏览器（等服务真正起来再开，否则打开是连接被拒）
3. `uvicorn.run(...)` 在主线程跑

**不 spawn 子进程**，也不依赖 `-m uvicorn` 这种模块路径。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser


def _open_browser_later(port: int) -> None:
    def worker() -> None:
        time.sleep(1.5)
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=worker, daemon=True).start()


def main() -> int:
    import uvicorn

    from app.main import create_app

    port = int(os.environ.get("JOBPILOT_PORT") or 8010)
    app = create_app()

    if os.environ.get("JOBPILOT_NO_BROWSER") != "1":
        _open_browser_later(port)

    print(f"jobpilot 正在启动：http://127.0.0.1:{port}")
    print("关闭这个窗口即停止服务。")
    uvicorn.run(app, host="127.0.0.1", port=port, workers=1, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
