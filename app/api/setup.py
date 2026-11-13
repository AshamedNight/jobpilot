"""首次启动向导接口（2026-11-13）。

三个动作，对应新用户的三步：

| 接口 | 作用 |
|---|---|
| `GET /api/setup/status` | 判断要不要显示向导 + 探测 Chrome + 给默认路径 |
| `POST /api/setup/apply` | 把三处路径写进 config.yaml |
| `POST /api/setup/launch-browser` | 用配置好的路径启动专用浏览器 |
| `GET /api/setup/verify-browser` | 探测调试端口是否活着（"我登录好了"按钮用）|

**只服务单账号首次配置**：向导不改 `pacing`/`quota`/`target`，
多账号配置仍然走 `config.yaml`（那是给会改配置的人用的）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body

from app.core import setup as setup_mod

router = APIRouter()


@router.get("/api/setup/status")
def api_setup_status() -> dict[str, Any]:
    return setup_mod.status()


@router.post("/api/setup/apply")
def api_setup_apply(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return setup_mod.apply(
        chrome_exe=str(payload.get("chrome_exe") or ""),
        profile_dir=str(payload.get("profile_dir") or ""),
        data_dir=str(payload.get("data_dir") or ""),
        cdp_port=int(payload.get("cdp_port") or 9333),
    )


@router.post("/api/setup/launch-browser")
def api_setup_launch(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    cfg = setup_mod.status()
    current = cfg.get("current") or {}
    return setup_mod.launch_browser(
        chrome_exe=str(payload.get("chrome_exe") or current.get("chrome_exe") or ""),
        profile_dir=str(payload.get("profile_dir") or current.get("chrome_profile") or ""),
        cdp_port=int(payload.get("cdp_port") or current.get("cdp_port") or 9333),
        url=str(payload.get("url") or "https://www.zhipin.com/web/geek/chat"),
    )


@router.get("/api/setup/verify-browser")
def api_setup_verify() -> dict[str, Any]:
    current = (setup_mod.status().get("current") or {})
    port = int(current.get("cdp_port") or 9333)
    alive = setup_mod.cdp_alive(port)
    return {
        "ok": alive,
        "port": port,
        "hint": "调试端口已连通 ✅" if alive else "连不上调试端口：确认浏览器是由本页启动的（带 --remote-debugging-port）",
    }
