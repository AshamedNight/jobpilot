"""FastAPI 入口。

启动命令：
    uvicorn app.main:app --host 127.0.0.1 --port 8010

只绑回环（架构 §1.4）。Phase 1 只有 fake 适配器；写 `adapter: boss` 会直接起不来，
这是**故意**的——接真 BOSS 是 Phase 2，现在放行只会让人以为能发请求。

【F6】建库放在 lifespan 启动事件里：`import app.main` 不得产生 `data/`。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, models
from app.adapters.boss import EXPECTED_UPSTREAM_VERSION, BossAdapter
from app.adapters.fake import FakeAdapter
from app.adapters.llm_gateway import LlmGateway
from app.api import connect, conversations, dashboard, direct_greet, pacing
from app.api import setup as setup_api
from app.config import StartupError, load_config
from app.core.composer import FakeLLM
from app.core.greet_pool import GreetPoolError, load_pool
from app.core.pacing import parse_config as parse_pacing_config
from app.services.pacer import build_pacer

WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "jobpilot.db"


def create_app(
    config_path: str | Path | None = None,
    matcher_path: str | Path | None = None,
    env_path: str | Path | None = None,
    db_path: str | Path | None = None,
    templates_path: str | Path | None = None,
    connect_runner: Any | None = None,
) -> FastAPI:
    """组装 app。所有依赖都在这里注入，方便测试传临时路径。

    注意：这个函数**不建库**（F6）。建库只在 lifespan 启动时做，这样 import 无副作用。
    """
    cfg = load_config(config_path, matcher_path, env_path)  # 校验不过 → StartupError → 起不来

    # 【补丁 §1.3】适配器选择必须接。之前只支持 fake，`ADAPTER=boss` 会被静默忽略
    #   （仍走 fake、还打假招呼），或直接 StartupError 起不来。
    #   零误发风险由 BossAdapter 的 greet/exchange NotImplementedError 保证，不靠"不接"。
    adapter_name = (os.environ.get("ADAPTER") or str(cfg.get("adapter", "fake"))).strip().lower()
    script = os.environ.get("JOBPILOT_FAKE_SCRIPT", "happy")  # 剧本可注入（Phase 2 风控测试复用）
    if adapter_name == "fake":
        adapter: Any = FakeAdapter(script=script)
    elif adapter_name == "boss":
        adapter = BossAdapter(cfg)
    else:
        raise StartupError(
            f"不支持的 adapter: {adapter_name!r}。可选：fake（本地剧本，默认）/ boss（真只读，P2-A）。"
        )
    # 【F7】probe() 的返回值必须覆盖 capabilities，否则探测等于没做
    #        （§19 第 9 条要验"capabilities 探测是否真的生效"）
    #        注意：BossAdapter.probe() 只调 schema（本地目录、不联网），不发业务请求。
    adapter.capabilities = set(adapter.probe())

    # 【F4】上游版本不符要告警（任务书 §3.3 Step 4，探针 G-5）。
    #   原来只落了 `/api/status.upstream_version`，`version_matches_expected` 是零调用点死代码：
    #   装了 3.0.2 时页面只显示 "3.0.2"，warnings 里什么都没有。
    #   注意 FakeAdapter 没有 `upstream_version`，必须 getattr + callable 兜底（同 F6 写法）。
    _upstream_value = getattr(adapter, "upstream_version", None)
    _upstream_value = _upstream_value() if callable(_upstream_value) else _upstream_value
    if _upstream_value and _upstream_value != EXPECTED_UPSTREAM_VERSION:
        cfg["warnings"] = list(cfg.get("warnings") or []) + [
            f"上游版本 {_upstream_value} 与预期的 {EXPECTED_UPSTREAM_VERSION} 不符："
            f"字段可能已变化，请核对 /api/status.upstream_version"
        ]

    # 【P2-B F6 · 2026-10-27 B-4a 事故】上游那把锁在**磁盘**上，不显示就等于没锁。
    #   现象：风控触发 → 重启服务 → `/api/status.risk_locked=false`、页面"运行中"，
    #   而上游每一次请求都返回 `ENVIRONMENT_RISK_LOCKED`「本次请求未发送」，`found=0`。
    #   用户会反复以为"已经恢复了、可以再跑"，每次重启只是清掉**我们内存里的**锁。
    #
    #   这里**只读本地文件**（零网络、不发请求），因此不违反"`probe()` 只许调 schema"。
    #   只在 boss 适配器下检查：上游锁是上游产物，FakeAdapter 根本不碰上游
    #   （否则仓库里那份 `~/.boss-agent-test/` 有锁就会把整个测试套件打成红的，
    #    正是 F1 刚修掉的"环境决定测试"）。
    upstream_risk_lock = False
    if adapter_name == "boss":
        data_dir = Path(
            str(((cfg.get("adapter_options") or {}).get("data_dir")) or "~/.boss-agent-test/")
        ).expanduser()
        if (data_dir / "cdp_risk_lock.json").exists():
            upstream_risk_lock = True
            cfg["warnings"] = list(cfg.get("warnings") or []) + [
                "上游本地风控锁仍在（cdp_risk_lock.json）：所有请求都会被拒"
                "（ENVIRONMENT_RISK_LOCKED），本服务重启也不能解除。"
                "请在官方页面确认风险解除后执行 boss clean --risk-lock，再重启本服务。"
            ]

    db_file = Path(db_path or os.environ.get("JOBPILOT_DB") or DEFAULT_DB_PATH)

    # 【P2-C】生成策略网关。模板资产缺失/为空 → StartupError，启动即失败（不许静默降级）。
    llm_gateway = LlmGateway(cfg, db_file, templates_path=templates_path)

    # 【P2-C ④.3】无 key **在启动时就告警**，不要等运行时才发现"怎么全是模板"。
    # 复用已有的 llm_configured 机制（.env 缺失 → 降级运行，接口照常可用）。
    if llm_gateway.mode in ("live", "hybrid") and not cfg.get("llm_configured", False):
        cfg["warnings"] = list(cfg.get("warnings") or []) + [
            f"llm.mode={llm_gateway.mode} 但 LLM_API_KEY 未配置："
            f"开局即回落模板（llm_calls 会记 template:fallback），接口照常可用"
        ]

    # 【闭环 1】招呼文案池：启动即校验（≥20 条、长度 ≤60、禁用词）。
    # 坏配置起不来——固定文案已经被证明是风控诱因，不许"池子坏了就退回那一句"。
    try:
        greet_pool = load_pool(cfg)
    except GreetPoolError as exc:
        raise StartupError(str(exc)) from exc

    # 【闭环 1】节奏参数同样启动即校验（时段/间隔下限/单轮上限/爬坡 ≤ 日额度）。
    # 这里只校验，不建 pacer：pacer 要读 app.state.db_path，得等 app 建好。
    if (cfg.get("pacing") or {}).get("enabled"):
        try:
            parse_pacing_config(
                cfg.get("pacing"), daily_cap=int((cfg.get("quota") or {}).get("daily_greet") or 0)
            )
        except StartupError:
            raise
        except Exception as exc:  # 兜底：不许带着坏节奏跑
            raise StartupError(f"节奏引擎配置不合法：{exc}") from exc

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """【F6】建库挪到这里：`python -c "import app.main"` 不该生成 data/。

        节奏引擎的启停也在这里：`start()` 要建 asyncio task，只有在事件循环里才行。
        默认**不自动开跑**（`pacing.auto_start=false`）——开机即发送的风险太大，
        闭环 5 做守护进程时再打开。
        """
        models.init_db(application.state.db_path).close()
        pacer = getattr(application.state, "pacer", None)
        if pacer is not None and bool((cfg.get("pacing") or {}).get("auto_start")):
            started = pacer.start()
            if not started.get("ok"):
                cfg["warnings"] = list(cfg.get("warnings") or []) + [
                    f"节奏引擎未能自动启动：{started.get('error')}"
                ]
        try:
            yield
        finally:
            if pacer is not None:
                pacer.stop(reason="服务关闭")

    app = FastAPI(
        title="jobpilot",
        version=__version__,
        description="求职助手 · Phase 1 · e2e 批（fake 全链路，ADAPTER=fake，全离线）",
        lifespan=lifespan,
    )
    app.state.config = cfg
    app.state.adapter = adapter
    # 【补丁 §1.3】记下**实际选中的**适配器名：`/api/status.adapter` 必须反映真相，
    # 否则 `ADAPTER=boss` 时页面还显示 fake（"看起来在跑其实是假的"）。
    app.state.adapter_name = adapter_name
    app.state.db_path = db_file
    app.state.paused = False
    # 【P2-C】生成策略网关：`writer_for(job)` 按岗位发一个 writer，
    # 编排层把它当 `llm` 用（`composer` 只认 `complete(prompt) -> str`，core 一行未改）。
    app.state.llm_gateway = llm_gateway
    # 【P2-C】`app.state.llm` 保留为**兼容别名**：Phase 1 的测试与外部脚本会读它。
    app.state.llm = FakeLLM()
    # 【闭环 1】招呼文案池（只读，构造时已校验）：direct_greet 与 pacer 共用同一份
    app.state.greet_pool = greet_pool
    # e2e 批：风控锁定与 paused 是两个独立开关，pause 不能清它
    # 【P2-B F6】启动时若上游磁盘锁还在，这里就必须是 True（不是恒 False）
    app.state.risk_locked = upstream_risk_lock
    app.state.upstream_risk_lock = upstream_risk_lock
    # 【单账号连接】连接管理器：状态纯内存、工作目录在数据库旁边（`connect/`），
    #   凭据目录 / 共享预算目录是两个独立绝对路径，绝不落到 `~/.boss-agent/`。
    #   runner 可注入（替身上游），生产默认 = 进程内 boss CLI。
    # 【R4】专用浏览器（B 路线）真实装配：只有本机两项配置齐备且合法才启用。
    #   缺配置/非法 → `UnavailableBrowser`：页面照常可用，点连接时**安全拒绝**
    #   （`browser_config:<码>`），不拖垮整个服务、不猜路径、不启动任何进程。
    connect_browser: Any | None = None
    if adapter_name == "boss":
        from app.services.connect_browser import (  # noqa: PLC0415 - 仅 boss 模式需要
            BrowserConfig,
            BrowserConfigError,
            ConnectBrowser,
            UnavailableBrowser,
        )

        try:
            connect_browser = ConnectBrowser(BrowserConfig.from_env())
        except BrowserConfigError as exc:
            connect_browser = UnavailableBrowser(str(exc))
    app.state.connect = connect.build_manager(
        cfg, db_file, runner=connect_runner, browser=connect_browser
    )

    # 【闭环 1】节奏推进器：只构造、不启动（启动入口是 POST /api/pacing/start）。
    #
    # 【闭环 3 · 2026-11-12】多账号并行时**必须**给每个账号独立的 state 文件：
    #   Pacer 的默认路径是 `db_path.parent / "pacer_state.json"` —— 多个账号的 db 都放在
    #   `data/` 下时，它们会共用同一个 state 文件：A 号熔断 24 小时，B 号跟着一起躺；
    #   A 号的 next_at 覆盖 B 号的 next_at。这是最隐蔽的一类串号。
    #   有账号时用 `pacer_state_<账号id>.json`；没有账号（测试/单实例默认）保持原路径。
    active_account = (cfg.get("active_account") or {}).get("id")
    pacer_state_path = None
    if active_account:
        pacer_state_path = db_file.parent / f"pacer_state_{active_account}.json"
    app.state.pacer = build_pacer(app, state_path=pacer_state_path)

    app.include_router(dashboard.router)
    app.include_router(conversations.router)
    # 【搜索直发】独立入口，不并入 dashboard：口径与 sweep 不同（不 detail/不算分/不走 LLM）
    app.include_router(direct_greet.router)
    # 【闭环 1】节奏引擎：看进度 / 开 / 关
    app.include_router(pacing.router)
    # 【2026-11-13】首次启动向导：把「改 YAML 路径」搬到页面上
    app.include_router(setup_api.router)
    # 【单账号连接】登录态入口：只开关登录态，不发送、不搜索（与 direct_greet 完全分开）
    app.include_router(connect.router)

    # 【连接 × 业务】双向互斥：连接中拒绝业务发送；业务运行中拒绝连接（在 manager 里判）。
    #   `/api/run/*` 的 POST 全算业务；只做放行/拒绝，不改任何业务实现。
    @app.middleware("http")
    async def _connect_business_mutex(request: Request, call_next):
        path = request.url.path
        if request.method == "POST" and path.startswith("/api/run/"):
            # 【F3 原子准入】"检查 + 登记"一次同锁完成，杜绝两次持锁之间的窗口；
            # 拒绝时不加计数，准入成功后 finally 必释放（异常也不泄漏、不负数）。
            manager = request.app.state.connect
            blocked = manager.acquire_business()
            if blocked:
                return JSONResponse(status_code=409, content={"detail": blocked})
            try:
                return await call_next(request)
            finally:
                manager.release_business()
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """单页 HTML，无构建步骤（§6.2）。"""
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        """service worker 必须从**根路径**返回，并且带 `Service-Worker-Allowed: /`。

        为什么不能直接放在 `/static/sw.js`：service worker 的默认作用域是它自己的目录，
        `/static/sw.js` 只能控制 `/static/` 下的请求 —— 首页 `/` 根本不受它管，
        离线缓存与"断网还能打开"全部失效（2026-11-12 前端 `已激活` 但离线的锅就是这里）。

        响应头 `Cache-Control: no-cache` 是为了让浏览器每次都回源校验脚本内容：
        SW 脚本自身被缓存住，改完不生效，是最难查的一类问题。
        """
        return FileResponse(
            WEB_DIR / "sw.js",
            media_type="application/javascript",
            headers={
                "Service-Worker-Allowed": "/",
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
        )

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover - 手动跑用
    import uvicorn

    server: dict[str, Any] = load_config().get("server") or {}
    uvicorn.run(
        "app.main:app",
        host=str(server.get("host", "127.0.0.1")),
        port=int(server.get("port", 8010)),
        reload=False,
    )
