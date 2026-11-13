# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（2026-11-13）。

## 三个必须知道的事实

1. **Chrome 不能打包进来**。它必须是真的 Chrome（独立 profile + 调试端口）。
   壳里塞 Chromium 会改变指纹，等于换了个运行环境 —— 这是这个项目的立身之本。
2. **`app/web` 必须打进去**。漏了就是"服务起来了但页面白屏"，而且用户完全猜不到原因。
3. **`config/matcher.yaml` 必须打进去**。`config.yaml` 里写着 `matcher_config` 指向它，
   漏了启动时就报配置错误。

## 用法

```bash
.venv2\\Scripts\\pyinstaller --noconfirm packaging/jobpilot.spec
# 产物：dist/jobpilot/jobpilot.exe（onedir，首次启动快）
```

用 `onedir` 不用 `onefile`：`onefile` 每次启动都要解压 ~60MB 到临时目录，
在机械硬盘上要 3–5 秒，用户体验上像是"双击没反应"。
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - PyInstaller 注入

datas = [
    (str(ROOT / "app" / "web"), "app/web"),                  # 页面（漏了就是白屏）
    (str(ROOT / "config" / "matcher.yaml"), "config"),        # 打分规则
    (str(ROOT / "config.example.yaml"), "."),                 # 首次运行复制成 config.yaml
    (str(ROOT / "requirements.txt"), "."),                    # 排障时能看到依赖清单
]

hiddenimports = [
    # uvicorn 的动态导入：漏任何一个都是"启动即退"，且报错信息很不直观
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "h11",
    # 上游 CLI 是 click 应用，且用 importlib 加载子命令
    "click",
    "boss_agent_cli",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PyQt5", "PySide2"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821  # PyInstaller 6.x 去掉了加密参数

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="jobpilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 压缩会被杀软误报，不值
    console=True,       # 保留控制台：用户能看到日志，排障成本低得多
    disable_windowed_traceback=False,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="jobpilot",
)
