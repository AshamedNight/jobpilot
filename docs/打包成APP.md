# 打包成 APP（分阶段方案）

> 2026-11-13。**结论先行**：引擎必须跑在有 Chrome 的机器上（电脑或云主机），
> 所以"APP"分两层——**外壳**可以打包，**引擎**搬不走。不要指望一个 APK 里既装浏览器又跑自动化。

## 一、三层结构

```
① PWA（已完成）        手机/桌面浏览器加主屏图标，零安装
② 桌面外壳             Tauri / WebView2 套壳 → jobpilot.exe，双击即用
③ 移动外壳             Capacitor 套壳 → APK（仍连本机服务，或用 Tailscale 打通）
```

三层的界面**完全复用**现有的 `app/web`（无构建步骤的静态页），只是外层容器不同。

## 二、现在就能做、且**不影响正在运行的引擎**

| 项 | 状态 | 说明 |
|---|---|---|
| 图标（192/512 PNG） | ✅ 已完成 | 从 `icon.svg` 导出，manifest 已接 |
| manifest 完整度（`id` / `lang` / maskable） | ✅ 已完成 | 安装体验与图标裁剪都对 |
| 对外 README + 免责 | ✅ 已完成 | 别人拿到就能看懂怎么跑 |
| 干净仓库自检 | ✅ 已完成 | `python -m app.tools.preflight_repo` |
| 一键启动 `.bat` | ✅ 已完成 | 见仓库根目录 `一键启动.bat` |
| 首次引导页（新手填路径） | 待做 | 纯前端，改完刷新即生效，**不重启服务** |
| PyInstaller spec 文件 | 待做 | **只写不跑**（跑起来抢 CPU，会拖慢 Chrome/CLI） |

**为什么这些安全**：它们要么是静态资源、要么是文档/脚本。uvicorn 没开 `--reload`，
写文件不会让运行中的进程重载；pacer 的节奏不受影响。

**真正不能现在做的**：改后端 Python 并**重启**（会打断正在发送的轮次）、真的执行打包（CPU 抢占）。

## 三、桌面壳（推荐先做）

| 方案 | 体积 | 优点 | 缺点 |
|---|---|---|---|
| **Tauri** | ~5MB | 小、原生 WebView2 | 需要 Rust 工具链 |
| **WebView2 + WinForms 单文件** | ~2MB | 只需 .NET | 代码偏老派 |
| **PyInstaller + pywebview** | ~40MB | 纯 Python 栈，和我们一致 | 首次启动慢 |
| **Electron** | ~150MB | 生态最全 | 太重，没必要 |

推荐 **Tauri**（或 PyInstaller+pywebview 求省事）：外壳只做三件事——
启动 `python -m app.tools.launch`、加载 `http://127.0.0.1:8010`、托盘图标显示状态。

**外壳不解包引擎**：它调用的是项目目录里的 `.venv2`，所以分发时是"目录 + exe"，
不是一个单文件。真要单文件，用 PyInstaller 打**整个服务**（见下）。

## 四、真正的"exe 单文件"（PyInstaller）

```
pyinstaller --noconfirm --onedir --name jobpilot ^
  --add-data "app/web;app/web" ^
  --add-data "config.example.yaml;." ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.lifespan.on ^
  app/main.py
```

三个必须知道的坑：

1. **Chrome 不能一起打包** —— 它必须是真的 Chrome（独立 profile + 调试端口），
   壳里塞 Chromium 会改变指纹，等于换了个环境
2. **`--add-data` 容易漏**：`app/web` 漏了就是白屏；`uvicorn.*` 漏了就是启动即退
3. **体积换算**：onedir ≈ 60MB，onefile ≈ 55MB 但每次启动要解压（慢 3–5 秒）

## 五、移动端（APK）

| 方案 | 装几个 | 依赖电脑 | 风控风险 |
|---|---|---|---|
| PWA 加主屏（现在） | 1 个图标 | 是 | 低 |
| **Capacitor 套壳**（连本机/云服务） | 1 个 APK | 是 | 低 |
| Auto.js 无障碍脚本 | 2 个 | 否 | **高** |
| 逆向 App 协议 | 1 个 | 否 | **最高** |

**Capacitor 套壳**是唯一"看起来像 APP、又不碰风控底线"的移动方案：

```bash
npm i -D @capacitor/cli @capacitor/core
npx cap init jobpilot com.example.jobpilot --web-dir=app/web
npx cap add android
npx cap open android     # 用 Android Studio 出 APK
```

但它仍然是"外壳 + 远程服务"：手机要有网络能到那台跑引擎的机器
（同机 `adb reverse`、或 Tailscale/Cloudflare Tunnel）。

## 六、建议的顺序

1. **先把 50 条/天跑稳**（现在卡在风控恢复，不在功能）
2. 首次引导页（1–2 小时，纯前端）
3. Tauri 桌面壳（半天）
4. Capacitor APK（1 天）
5. 云端部署（可选，半天）——让手机脱离家里电脑

**第 1 步不做完，后面四步都是空转**：壳做得再漂亮，引擎一天被锁三次也没东西可看。
