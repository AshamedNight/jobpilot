"""单账号「连接 BOSS」：状态查询 + 发起连接（任务书-单账号前端简单连接 §4/§5）。

本模块**只管登录态**，一行发送逻辑都没有：不 search、不 detail、不 greet、
不评分、不碰 sweep。连接成功也**不**等于发送授权。

边界（任务书 §5，逐条对应）：

1. **只本机**：`Host` 必须回环，`Origin` 若存在也必须回环 → 否则 403。
   不加 CORS、不设 `*`、不新增无鉴权远程管理入口、不拼 shell、不用 `shell=True`。
2. **单任务互斥**：进程内一条登录任务，带租约（`login_timeout + grace`）。
   重复点击在 `connecting` 期间一律拒绝（409，只表示状态冲突），不会再开一个浏览器。
   租约过期自动转 `timeout` 并释放 → 线程异常死亡也不会留下永远卡住的 `connecting`。
   状态**纯内存**，进程重启即 `disconnected`，不会残留无法清理的状态。
3. **"登录完成"有明确判据**：登录信封 ok → 凭据文件非空 → 本地登录态可确认 →
   账号身份核验通过，四道全过才算 `connected`。
   **不接受**"浏览器打开了" / "session 文件存在" 冒充（上游 `TokenStore.load()`
   只要文件能解密就返回 token，文件存在 ≠ 有效登录）。
4. **旧凭据必须保住**：上游 `TokenStore.save()` 是裸 `write_bytes()`（非原子、无回滚），
   所以本项目侧自己做备份 + 失败回滚：失败 / 超时 / 半写 / 同步不完整一律还原旧
   `session.enc`，并用字节相等证明"旧凭据完整保留"。
5. **账号身份无法确认就停**：身份来源只有 `boss me --section user`，本地只存
   `HMAC-SHA256(本机密钥, 稳定字段)` 摘要，摘要不进响应、不进日志。
   拿不到身份 → `needs_user_action/account_identity_unverified`；与既有绑定不一致
   → `needs_user_action/account_mismatch`（不覆盖旧绑定、不自动修复）。
6. **不自动清锁**：不调用 `clean`、不传 `--risk-lock` / `--force` / `--ignore-risk-lock`，
   发现风险锁直接 503 + 指向既有恢复流程；**不自动重试**。

状态集（固定）：`disconnected` / `connecting` / `connected` / `failed` /
`needs_user_action` / `timeout`。其中 `failed` 与 `timeout` 是**两个不同状态**，
不许合成一个"失败了"。

【初审阻塞项修回 · 三件事】

A. **超时 ≠ 空闲**：租约到期只把*展示状态*改成 `timeout`，**不**释放占用。
   旧线程只要还没退出，`start()` 继续拒（409）、业务发送继续拒（409），
   因为那一刻上游登录可能正在写凭据。`task_active` 的语义 = "有连接任务在占用"
   （`connecting` **或** 旧线程仍在跑）。
B. **迟到返回不污染**：租约到期时把任务标记为"旧任务"（`_task_id = None`）。
   旧线程回来时只做一件事——把自己可能写下的凭据**撤回**到连接前快照；
   状态、`reason`、账号绑定一律不动，写 `account_binding.json` 之前还有一道
   `_is_stale()` 守卫。
C. **失败一律恢复连接前凭据**：身份未确认 / 首次绑定未确认 / 账号不匹配 /
   超时 / 上游错误，全部走 `_restore_credentials()`：连接前有旧 `session.enc`
   → 字节还原；**连接前没有 → 删除本次新写的 `session.enc`**
   （不留可被业务继续使用的登录态）。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.adapters.boss import _CLI_LOCK, _cli_entry, normalize_error_code

router = APIRouter(tags=["connect"])

# --------------------------------------------------------------------------- #
# 状态常量：前端与测试都按字符串比，不引入枚举层
# --------------------------------------------------------------------------- #
STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_FAILED = "failed"
STATE_NEEDS_USER_ACTION = "needs_user_action"
STATE_TIMEOUT = "timeout"

#: 允许的 Host / Origin 主机名。**只放回环**（含 `[::1]` 去括号后的 `::1`）。
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

#: 上游风控锁文件名（`boss status` 里 `cdp_risk_lock` 那一份）。
RISK_LOCK_FILE = "cdp_risk_lock.json"

_CONNECT_DIR = "connect"
#: 【F1】上游 zhipin 真实凭据布局（`auth/manager.py:30-34`）：`--data-dir` 只是**根**，
#: `TokenStore` 落在 `<data-dir>/auth/`；`auth/token_store.py:20-26` 三个文件名
#: `session.enc` / `salt` / `refresh.lock`。
AUTH_SUBDIR = "auth"
_SESSION_FILE = "session.enc"
_SALT_FILE = "salt"
_BACKUP_FILE = "session.enc.bak"
_SALT_BACKUP_FILE = "salt.bak"
_BINDING_FILE = "account_binding.json"
_SECRET_FILE = "secret.key"
#: 【R1】任务开始的持久记录：登录前写，成功提交或核验恢复完成后才删。
#: 进程在“已改写真实凭据、尚未提交/回滚”之间中断时，新 manager 靠它阻断放行。
_INPROGRESS_FILE = "in_progress.json"
#: 【R1】恢复标记里的 reason 取值（只放安全码，不放路径/账号）。
RECOVERY_REASON_INTERRUPTED = "interrupted_task"
RECOVERY_REASON_ORPHAN_BACKUP = "orphan_backup"


class ConnectConflict(Exception):
    """状态冲突（已有连接任务 / 业务运行中）→ 409。"""


class ConnectBlocked(Exception):
    """风险锁等外部阻断 → 503（不是状态冲突，不用 409）。"""


class BackupCleanupError(OSError):
    """【fix3c·缺陷1】收尾时清理本任务备份失败。

    单独一个异常类型，是为了让提交路径能把它和「绑定写入失败」分开报：
    两者都保持阻断，但原因与提示不能混（混了就会出现"新绑定 + 旧凭据"却只说绑定失败）。
    """


class ConnectStartBody(BaseModel):
    """发起连接的请求体。`confirm_bind` 只用于**首次**绑定账号（默认 False）。"""

    confirm_bind: bool = False


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _host_only(value: str) -> str:
    """`127.0.0.1:8010` / `[::1]:8010` / `localhost` → 纯主机名（小写、去括号）。"""
    text = (value or "").strip().lower()
    if text.startswith("["):
        end = text.find("]")
        return text[1:end] if end > 0 else text.strip("[]")
    return text.rsplit(":", 1)[0]


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """同目录 tmp + `os.replace`：写一半断电也不会留下半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    with contextlib.suppress(OSError):
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def _norm_path(value: str | Path) -> str:
    """规范绝对路径比较：Windows 上大小写/分隔符等价，不靠字符串裸比。"""
    text = os.path.normcase(os.path.normpath(str(Path(value).expanduser())))
    return text.rstrip("\\/") or text


class BudgetConfigError(Exception):
    """上游 `config.json` 不可读/相对路径/损坏 —— 一律拒绝，不自动写配置。"""


def upstream_budget_dir(cred_dir: Path) -> Path:
    """按**上游固定规则**算真实预算目录（`api/request_budget.py:154-165`）。

    【R3】上游要求 `config.json` 是 JSON **对象**，`request_budget_dir` 只能是
    `null` 或**非空绝对路径字符串**。因此这里严格区分三种情况，不再一律"当缺失"：

    - 键缺失 / 值为 `None` → 回退凭据根（上游同义）
    - 值非法（空串、`0`、`False`、数字、列表、对象…）→ 抛错，**不回退**
    - 相对路径 / 配置损坏 → 抛错

    上一版用 `if not value` + `Path(str(value))`：空串/0/False 被当缺失、
    非字符串被 `str()` 转换，比上游宽松 —— 会让"env 被清空/漂移"悄悄落到另一个预算目录。
    """
    config_path = Path(cred_dir) / "config.json"
    if not config_path.exists():
        return Path(cred_dir).resolve()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BudgetConfigError(type(exc).__name__) from exc
    if not isinstance(payload, dict):
        raise BudgetConfigError("not_object")
    if "request_budget_dir" not in payload:
        return Path(cred_dir).resolve()
    value = payload["request_budget_dir"]
    if value is None:
        return Path(cred_dir).resolve()
    if not isinstance(value, str) or not value.strip():
        raise BudgetConfigError("invalid_type")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise BudgetConfigError("relative_request_budget_dir")
    return candidate.resolve()


def _is_complete_cn_mobile(value: str) -> bool:
    """完整中国大陆手机号（11 位、1 开头、第二位 3-9）。掩码/残缺/异常一律 False。"""
    text = value.strip()
    if len(text) != 11 or not text.isdigit():
        return False
    return text[0] == "1" and text[1] in "3456789"


def _safe_code(raw: Any) -> str:
    """上游错误码只留 `[A-Z0-9_]`：码是安全信息，正文不是。"""
    text = str(raw or "").strip().upper()
    cleaned = "".join(ch for ch in text if ch.isalnum() or ch == "_")[:40]
    return cleaned or "UNKNOWN"


def _require_local(request: Request) -> None:
    """本机校验：Host 必须回环；Origin 若存在也必须回环。"""
    if _host_only(request.headers.get("host", "")) not in LOOPBACK_HOSTS:
        raise HTTPException(status_code=403, detail="只允许本机访问（Host 不是回环地址）")
    origin = request.headers.get("origin")
    if origin:
        parts = urlsplit(origin)
        if parts.scheme not in {"http", "https"} or (parts.hostname or "").lower() not in LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="拒绝跨站请求（Origin 不是本机回环地址）")
    # 跨站表单 POST 会带 `sec-fetch-site: cross-site`，本机页面不会
    if (request.headers.get("sec-fetch-site") or "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="拒绝跨站请求（sec-fetch-site=cross-site）")


# --------------------------------------------------------------------------- #
# 上游 runner：唯一碰 CLI 的地方（测试注入替身替换整个对象，不是分支）
# --------------------------------------------------------------------------- #
class UpstreamLoginRunner:
    """生产实现：进程内调 `boss login` / `boss status` / `boss me`。

    与 `app/adapters/boss.py` 共用同一个模块级 `_CLI_LOCK`
    （`redirect_stdout` 动的是进程全局 `sys.stdout`，必须串行）。
    **不使用 subprocess、不使用 shell**；`--data-dir` 自己算绝对路径并显式传，
    绝不落到默认的 `~/.boss-agent/`（主号登录态）。
    """

    def _base(self, cred_dir: Path) -> list[str]:
        # 全局选项必须在子命令**前面**（Step0 §4.1），`--json` 紧随其后
        return ["--data-dir", str(Path(cred_dir).resolve()), "--json"]

    def _run(self, argv: list[str]) -> dict[str, Any]:
        cli = _cli_entry()
        buffer = io.StringIO()
        # 【S2.3】stderr 也接进内存缓冲：上游会往 stderr 打账号指纹/路径，
        # 直接继承父进程 stderr 等于把它们灌进服务日志。这里只**丢弃**内容，
        # 只保留"有没有输出"这一安全事实（不落盘、不回传、不进响应）。
        err_buffer = io.StringIO()
        with _CLI_LOCK, contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(err_buffer):
            try:
                cli(args=argv, prog_name="boss", standalone_mode=True)
            except SystemExit:
                # 业务错误也是 SystemExit(1) + 完整信封 → 继续解 stdout
                pass
            except Exception as exc:  # noqa: BLE001 - 只回类型名，不回正文
                return {"ok": False, "error": {"code": "UPSTREAM_ERROR", "message": type(exc).__name__}}
        text = (buffer.getvalue() or "").strip()
        if not text:
            return {"ok": False, "error": {"code": "UPSTREAM_ERROR", "message": "上游无输出"}}
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            return {"ok": False, "error": {"code": "UPSTREAM_ERROR", "message": "上游输出不是 JSON"}}
        if not isinstance(envelope, dict):
            return {"ok": False, "error": {"code": "UPSTREAM_ERROR", "message": "上游信封形状异常"}}
        return envelope

    # -- 受控 CDP 同步（复审·缺陷2）--------------------------------------- #
    def _auth_manager(self, cred_dir: Path):
        """固定上游的 `AuthManager`（只为拿它有源码依据的锁检查/风险记录/`TokenStore`）。"""
        from boss_agent_cli.auth.manager import AuthManager  # noqa: PLC0415

        return AuthManager(Path(cred_dir))

    def _login_via_cdp(self, **kwargs):
        """固定上游的 `login_via_cdp`（唯一允许的 CDP 同步入口，`reuse_existing=True`）。"""
        from boss_agent_cli.auth.browser import login_via_cdp  # noqa: PLC0415

        return login_via_cdp(**kwargs)

    def _controlled_cdp_sync(self, *, cred_dir: Path, cdp_url: str, timeout_s: int) -> dict[str, Any]:
        """受控 CDP 同步（复审·缺陷2 的唯一实现面）。

        走固定上游 `auth/browser.py:297 login_via_cdp(reuse_existing=True)`：

        - `_probe_reused_session`（`:129`）是上游自带**只读**浏览器会话探测；
          判定失效就抛 `ReusedSessionStaleError`（`:434`）且**不落盘** → 这里转手动码；
        - 风险锁/共享预算用上游 `_check_cdp_login_risk_lock`（`:103`）前置；
        - 风险用 `_record_cdp_risk_lock`（`:62`）记录；
        - 落盘只用上游 `TokenStore.save`。

        **禁止**碰 `AuthManager._cdp_login`（它在复用失效后会改走不复用分支并清目标平台 cookie），
        也**禁止**调高层 `boss login --cdp` CLI。本方法只以复用模式调用，不出现强制重登开关。
        """
        stale_type = self._stale_error_type()
        risk_type = self._risk_error_type()
        manager = self._auth_manager(cred_dir)

        # 上游风险锁 + 共享预算前置（只读，不连 CDP、不因新令牌解锁）
        try:
            manager._check_cdp_login_risk_lock(cdp_url or None)
        except risk_type as exc:
            return {"ok": False, "error": {"code": _safe_code(getattr(exc, "code", None)) or "RISK_LOCKED",
                                           "message": "风险锁或预算控制生效：已停止，不清锁、不重试"}}
        except Exception as exc:  # noqa: BLE001 - 只出类型名
            return {"ok": False, "error": {"code": "RISK_CHECK_FAILED", "message": type(exc).__name__}}

        # stderr 继续只捕获丢弃：上游会往 stderr 打账号指纹/路径
        err_buffer = io.StringIO()
        try:
            with _CLI_LOCK, contextlib.redirect_stderr(err_buffer):
                token = self._login_via_cdp(
                    cdp_url=cdp_url or None,
                    timeout=int(timeout_s),
                    platform="zhipin",
                    reuse_existing=True,
                )
        except stale_type:
            # 上游只读探测判定复用的登录态失效：**不落盘、不清 cookie、不重登**
            return {
                "ok": False,
                "error": {
                    "code": "SESSION_STALE_MANUAL",
                    "message": "浏览器里复用的登录态已在服务端失效：已停止，未清 Cookie、未重登；"
                               "请在专用浏览器里重新扫码后重试",
                },
            }
        except risk_type as exc:
            with contextlib.suppress(Exception):
                manager._record_cdp_risk_lock(cdp_url or None, error=exc)
            return {"ok": False, "error": {"code": _safe_code(getattr(exc, "code", None)) or "RISK_LOCKED",
                                           "message": "登录过程命中平台风险控制：已停止并记录，不重试"}}
        except TimeoutError:
            return {"ok": False, "error": {"code": "LOGIN_TIMEOUT", "message": "登录超时"}}
        except Exception as exc:  # noqa: BLE001 - 只出类型名，正文不进响应
            return {"ok": False, "error": {"code": "UPSTREAM_ERROR", "message": type(exc).__name__}}

        if not isinstance(token, dict) or not token:
            return {"ok": False, "error": {"code": "SESSION_INCOMPLETE", "message": "上游未返回可用令牌"}}
        try:
            manager._store.save(token)
        except OSError as exc:
            return {"ok": False, "error": {"code": "TOKEN_SAVE_FAILED", "message": type(exc).__name__}}
        return {"ok": True, "data": {"logged_in": True}}

    def _stale_error_type(self):
        from boss_agent_cli.auth.browser import ReusedSessionStaleError  # noqa: PLC0415

        return ReusedSessionStaleError

    def _risk_error_type(self):
        from boss_agent_cli.api.client import PlatformRiskError  # noqa: PLC0415

        return PlatformRiskError

    def login(self, *, cred_dir: Path, budget_dir: Path, cdp_url: str, timeout_s: int) -> dict[str, Any]:
        """受控登录入口（复审·缺陷2）。

        固定上游 `auth/manager.py:45-57` 在 `ReusedSessionStaleError` 后会改走
        不复用分支**再登录一次**并清掉目标平台 cookie。因此这里**不**走 `_cdp_login`、
        **不**走 `boss login --cdp` CLI，只以复用模式调用 `login_via_cdp`：

        - 浏览器会话有效（含存量凭据）→ 同步并 `TokenStore.save`；
        - 浏览器会话失效 → 上游抛 `ReusedSessionStaleError` → 手动码，零落盘、零 cookie 清除；
        - 首次人工扫码 → 同一条路径即可完成（上游正常走扫码）。

        「本地有 session」不再被当作「可以复用」的依据：两者是不同事实。
        """
        return self._controlled_cdp_sync(cred_dir=cred_dir, cdp_url=cdp_url, timeout_s=timeout_s)

    def local_status(self, *, cred_dir: Path) -> dict[str, Any] | None:
        """`boss status`（**默认只查本地**，不 `--live`、不联网、不发业务请求）。"""
        return self._run([*self._base(cred_dir), "status"])

    def identity_source(self, *, cred_dir: Path) -> str | None:
        """`boss me --section user` → **带命名空间**的稳定身份输入，或 `None`（无法确认）。

        【S1】证据边界（`commands/me.py:36-43`，固定 3.0.1）：
        `me` 的 user 白名单只有 `name / email / phone / identity / avatar`，
        **没有** `userId` / `uid` / `encryptUserId`。因此：

        - 只有 `phone` 通过"完整手机号"形态校验时才能当本地 HMAC 输入；
        - `identity` 是角色字段（如「求职者」），**不是**账号唯一标识 → 一律不可用；
        - 姓名 / 头像 / 邮箱 / 掩码手机号 → 一律 `None`（上层落 `account_identity_unverified`）。

        这里的 `None` 是**精确拒绝**，不代表可用闭环已完成（见交付说明的待 Windows 核验项）。
        """
        envelope = self._run([*self._base(cred_dir), "me", "--section", "user"])
        if not isinstance(envelope, dict) or envelope.get("ok") is False:
            return None
        data = envelope.get("data")
        user = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict):
            return None
        phone = str(user.get("phone") or "").strip()
        if not _is_complete_cn_mobile(phone):
            return None
        # 命名空间：不同字段同值也不会碰撞
        return f"v2:phone-cn:{phone}"


# --------------------------------------------------------------------------- #
# 连接管理器
# --------------------------------------------------------------------------- #
class ConnectManager:
    """连接任务状态机 + 单任务互斥 + 旧凭据保护 + 账号绑定核验（全部进程内）。"""

    def __init__(
        self,
        *,
        data_dir: Path,
        cred_dir: Path,
        budget_dir: Path,
        runner: Any | None = None,
        cdp_url: str = "http://127.0.0.1:9222",
        login_timeout_s: int = 120,
        lease_grace_s: int = 30,
        path_problem: str | None = None,
        strict_paths: bool = False,
        browser: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cred_dir = Path(cred_dir)
        self.budget_dir = Path(budget_dir)
        self.runner = runner if runner is not None else UpstreamLoginRunner()
        self.cdp_url = str(cdp_url or "")
        self.login_timeout_s = int(login_timeout_s)
        self.lease_grace_s = int(lease_grace_s)
        self._clock = clock
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))

        self._lock = threading.RLock()
        self._state = STATE_DISCONNECTED
        self._reason = "not_started"
        self._message = "尚未连接：点击「连接 BOSS」，在打开的浏览器里完成扫码/验证。"
        self._updated_at = self._stamp()
        self._task_id: str | None = None
        self._deadline: float | None = None
        self._binding = "not_applicable"
        self._session_preserved: bool | None = None
        self._business_inflight = 0
        #: 【F4】启动时算出来的路径/预算问题码（None = 合法）
        self.path_problem = path_problem
        #: 【F4.5】只有 boss 模式做严格路径/预算校验；fake 模式保持原离线语义
        self.strict_paths = bool(strict_paths)
        #: 【S2】专用浏览器（`app/services/connect_browser.py`）；None = 不启用浏览器能力
        self.browser = browser
        #: 【F2】恢复待办：启动先读盘，有就不许连接、不许业务发送
        self._recovery_reason: str | None = self._read_recovery_todo()
        self._recovery_persisted: bool = True
        #: 【修回 A】worker 是否仍占用。租约到期**不**清它 —— 只要线程还活着，
        #: 新的连接与业务发送都必须继续被拒（上游登录可能正在写凭据）。
        self._worker_alive = False
        #: 本次尝试前的旧凭据字节（迟到/失败分支回滚用）
        self._before: bytes | None = None
        #: 【F1】连接前的 session+salt 快照（迟到/异常兜底恢复用）
        self._snapshot: dict[str, bytes | None] | None = None
        #: 【修回 B · 副作用单点】成功时由 `_attempt` 写入"待提交动作"（写绑定 / 删备份），
        #: 真正执行只在 `_run_task` 的 finally 里、且只在"本任务仍是当前任务"时。
        #: 迟到任务一律不写绑定、不删备份。
        self._commit: dict[str, Any] | None = None

    # -- 路径 ------------------------------------------------------------ #
    def _work_dir(self) -> Path:
        return self.data_dir / _CONNECT_DIR

    def auth_dir(self) -> Path:
        """真实凭据目录 = `<cred_dir>/auth`（上游 zhipin 布局，**不是**根目录）。"""
        return self.cred_dir / AUTH_SUBDIR

    def _session_file(self) -> Path:
        return self.auth_dir() / _SESSION_FILE

    def _salt_file(self) -> Path:
        return self.auth_dir() / _SALT_FILE

    def _backup_file(self) -> Path:
        return self._work_dir() / _BACKUP_FILE

    def _salt_backup_file(self) -> Path:
        return self._work_dir() / _SALT_BACKUP_FILE

    def _binding_file(self) -> Path:
        return self._work_dir() / _BINDING_FILE

    # -- 快照 ------------------------------------------------------------ #
    def _stamp(self) -> str:
        return self._utcnow().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _snapshot_locked(self) -> dict[str, Any]:
        state, reason, message = self._state, self._reason, self._message
        if self._recovery_reason and state == STATE_DISCONNECTED:
            # 【F2.3】有未完成恢复待办时，只读状态也不许显示成"未连接/可再来一次"
            state = STATE_NEEDS_USER_ACTION
            reason = "recovery_todo_present"
            message = ("存在未完成的连接恢复待办：请人工核对原凭据与原账号后处理，"
                       "本次进程内连接与业务发送均被拒绝（不自动清风险锁、不换账本）。")
        return {
            "state": state,
            "reason": reason,
            "message": message,
            "updated_at": self._updated_at,
            # 【修回 A】"有任务占用"= 正在连接 **或** 旧线程还没退出
            "task_active": self._state == STATE_CONNECTING or self._worker_alive,
            "cancel_supported": False,  # 上游登录不可中断，见任务书 §4：不假装能取消
            "binding": self._binding,
            "session_preserved": self._session_preserved,
        }

    def _apply_locked(self, *, state: str, reason: str, message: str,
                      binding: str, session_preserved: bool | None) -> None:
        self._state = state
        self._reason = reason
        self._message = message
        self._binding = binding
        self._session_preserved = session_preserved
        self._updated_at = self._stamp()

    def _expire_locked(self) -> None:
        """租约定时到期：`connecting` 挂太久就判 `timeout` 并释放（内存态不会卡死）。"""
        if self._state != STATE_CONNECTING or self._deadline is None:
            return
        if self._clock() <= self._deadline:
            return
        # 【修回 A/B】只把展示状态改成 timeout，**不释放占用**：
        # 线程还在跑时上游可能正在写凭据，此刻放行第二个连接或业务发送最危险。
        # 同时把任务标记为"旧任务"（`_task_id = None`），迟到返回只能撤回自己的改动。
        self._task_id = None
        self._deadline = None
        self._apply_locked(
            state=STATE_TIMEOUT,
            reason="login_timeout",
            message="连接任务超时：旧任务仍在运行，已禁止新的连接与业务发送；"
                    "请等它结束，或按既有流程停止服务。凭据状态待任务结束后核验，"
                    "本服务不宣称旧登录态未被改动。",
            binding="not_applicable",
            session_preserved=None,
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._expire_locked()
            return self._snapshot_locked()

    # -- 恢复待办（F2.3–2.5）--------------------------------------------- #
    def _path_drift_reason(self) -> str | None:
        """【F4.3】每次准入都重查：安全路径 + 共享预算 + 运行中漂移。

        不支持运行中热切换：检测到环境变量被改 → 停并要求重启核验。
        """
        if self.path_problem:
            return f"安全路径未通过校验（{self.path_problem}）：请修正本机配置后重启服务"
        if not self.strict_paths:
            # fake 模式：不读真实 boss 目录、不要求 cred/budget env（离线语义不变）
            return None
        for name, expected in (("JOBPILOT_BOSS_CRED_DIR", self.cred_dir),
                               ("JOBPILOT_BOSS_BUDGET_DIR", self.budget_dir)):
            raw = (os.environ.get(name) or "").strip()
            if raw and _norm_path(raw) != _norm_path(expected):
                return f"{name} 在运行中被改变：已停止，请重启并重新核验（不支持热切换）"
        try:
            effective = upstream_budget_dir(self.cred_dir)
        except BudgetConfigError as exc:
            return f"共享预算配置不可用（{type(exc).__name__}）：拒绝执行，请人工修复后重启"
        if _norm_path(effective) != _norm_path(self.budget_dir):
            return "实际共享预算目录与期望不一致：拒绝执行（不创建新空预算、不改配置）"
        return None

    def _recovery_file(self) -> Path:
        return self._work_dir() / "recovery_required.json"

    def _inprogress_file(self) -> Path:
        """【R1】任务开始记录（`in_progress.json`）。存在即代表“有未完成连接任务”。"""
        return self._work_dir() / _INPROGRESS_FILE

    def _read_recovery_todo(self) -> str | None:
        # 【R1】三源判定（fail-closed，任一命中即阻断连接与相关 boss 业务）：
        #   1) recovery_required.json —— 显式恢复待办（原逻辑保留）
        #   2) in_progress.json —— 任务已开始但没走到“提交完成/核验恢复完成”
        #      （进程在 login 改写真实凭据之后被中断，正是复审说的绕过口）
        #   3) 有 .bak 却没有任何标记 —— 旧备份残留，不许被当成本次恢复依据
        if self._inprogress_file().exists():
            return RECOVERY_REASON_INTERRUPTED
        path = self._recovery_file()
        if not path.exists():
            if self._backup_file().exists() or self._salt_backup_file().exists():
                return RECOVERY_REASON_ORPHAN_BACKUP
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 标记损坏 → 按"有未完成恢复"处理（fail-closed），不许当成没有
            return "recovery_marker_unreadable"
        reason = payload.get("reason") if isinstance(payload, dict) else None
        return str(reason or "recovery_marker_unknown")

    # -- 任务开始记录（R1）------------------------------------------------- #
    def _begin_task_record(self, task_id: str, snapshot: dict[str, bytes | None]) -> None:
        """【R1】登录等可能改凭据的动作**之前**持久记录任务开始。

        记录内容只放安全元数据（task_id / 时间 / 两个文件“连接前是否存在”），
        不放路径、账号、摘要、cookie。写不进去就向上抛 —— 调用方必须拒绝进入登录：
        否则进程在“已改写真实凭据”之后被中断，新 manager 无从判断到底动没动过。

        同时**先清掉旧备份**：上一次任务留下的 `.bak` 不许被当成本次的恢复依据。
        """
        for stale in (self._backup_file(), self._salt_backup_file()):
            with contextlib.suppress(OSError):
                stale.unlink(missing_ok=True)
        payload = {
            "task_id": task_id,
            "started_at": self._stamp(),
            "session_existed": snapshot["session"] is not None,
            "salt_existed": snapshot["salt"] is not None,
            "phase": "pre_login",
        }
        _atomic_write(self._inprogress_file(), json.dumps(payload).encode("utf-8"))

    def _clear_inprogress(self) -> None:
        """【R1】任务记录只允许在两种结局后消失：提交成功、或恢复**读回核验通过**。

        删除失败不算清理完成 —— 保留记录即保持阻断（宁可多阻，不可放行）。
        """
        try:
            self._inprogress_file().unlink(missing_ok=True)
        except OSError:
            return
        # 只有在没有显式恢复待办时才把内存里的阻断原因一并清掉：
        # `recovery_required.json` 存在 = 仍需人工处理，不许被“任务记录清掉”顺手放行。
        if not self._recovery_file().exists():
            self._recovery_reason = None

    def _set_recovery_todo(self, reason: str) -> None:
        """落最小持久恢复待办。落盘失败 → **当前进程立即阻断**，且提示不得靠重启恢复。"""
        self._recovery_reason = reason
        try:
            _atomic_write(
                self._recovery_file(),
                json.dumps({"reason": reason, "at": self._stamp()}).encode("utf-8"),
            )
            self._recovery_persisted = True
        except OSError:
            # 【F2.5】写不进去也必须阻断；此时只能靠人工核对，不能靠重启
            self._recovery_persisted = False

    def _clear_recovery_todo(self) -> None:  # pragma: no cover - 只由人工流程调用
        with contextlib.suppress(OSError):
            self._recovery_file().unlink(missing_ok=True)
        self._recovery_reason = None
        self._recovery_persisted = True

    # -- 业务互斥（F3 原子准入）------------------------------------------- #
    def acquire_business(self) -> str | None:
        """【F3】"检查 + 登记"一次同锁原子完成：返回拒绝原因或 None。

        上一版是 `blocked_reason_for_business()` 与 `business_enter()` 两次分别持锁，
        两个调用之间存在窗口；这里合并成一次，拒绝时**不加计数**。
        """
        with self._lock:
            self._expire_locked()
            reason = self._business_block_reason_locked()
            if reason:
                return reason
            self._business_inflight += 1
            return None

    def release_business(self) -> None:
        """准入成功后必须 finally 调用；异常也不许泄漏计数、不许负。"""
        with self._lock:
            self._business_inflight = max(0, self._business_inflight - 1)

    def business_enter(self) -> None:  # 兼容旧调用面（内部同样走一次同锁）
        with self._lock:
            self._business_inflight += 1

    def business_exit(self) -> None:
        with self._lock:
            self._business_inflight = max(0, self._business_inflight - 1)

    def _business_block_reason_locked(self) -> str | None:
        if self._state == STATE_CONNECTING:
            return "连接任务进行中：业务发送已拒绝，请等连接结束（连接成功不代表发送授权）"
        if self._worker_alive:
            # 超时后旧线程仍在跑：同样不许放行业务发送
            return "上一轮连接任务仍未退出（已超时）：业务发送已拒绝，请等它结束或停止服务"
        if self._recovery_reason:
            return ("存在未完成的连接恢复待办：业务发送已拒绝，"
                    "请先按连接页提示人工核对原凭据与原账号，再重启服务")
        # 【R3】锁判定要排在"路径漂移"之前：锁是用户当下必须先处理的事，
        #   而路径漂移文案会把它盖住，导致两入口给出的理由不一致。
        #   仅 boss 严格模式生效：fake 不读真实 boss 目录，也不该被真实锁文件污染。
        if self.strict_paths and self._risk_lock_file() is not None:
            return ("风险锁存在（cdp_risk_lock.json）：业务发送已拒绝，不清锁、不重试，"
                    "请人工按既有恢复流程处理后重启服务")
        drift = self._path_drift_reason()
        if drift:
            return drift
        return None

    def blocked_reason_for_business(self) -> str | None:
        """只读检查（不加计数）；准入请用 `acquire_business()`。"""
        with self._lock:
            self._expire_locked()
            return self._business_block_reason_locked()

    # -- 风险锁（只读、只用于拒绝） -------------------------------------- #
    def _risk_lock_file(self) -> Path | None:
        """两个目录都看：凭据目录 + 共享预算目录。任一有锁即停。"""
        for base in (self.cred_dir, self.budget_dir):
            candidate = Path(base) / RISK_LOCK_FILE
            if candidate.exists():
                return candidate
        return None

    # -- 发起 ------------------------------------------------------------ #
    def start(self, *, confirm_bind: bool = False) -> dict[str, Any]:
        with self._lock:
            self._expire_locked()
            if self._state == STATE_CONNECTING:
                raise ConnectConflict("已有连接任务，请等待完成（本机同时只允许一个连接任务）")
            if self._worker_alive:
                # 【修回 A】超时后旧线程仍在跑 → 不许开第二个登录任务（两个登录会抢同一份凭据）
                raise ConnectConflict("上一轮连接任务仍未退出（已超时）：请等它结束或按既有流程停止服务")
            if self._business_inflight > 0:
                raise ConnectConflict("业务任务运行中：连接请求已拒绝，请等本轮业务结束")
            if self._recovery_reason:
                self._apply_locked(
                    state=STATE_NEEDS_USER_ACTION,
                    reason="recovery_todo_present",
                    message="存在未完成的连接恢复待办：请先人工核对原凭据与原账号（不自动清风险锁、"
                            "不换账本），处理完再重启服务；本次连接已拒绝。",
                    binding="not_applicable",
                    session_preserved=None,
                )
                raise ConnectBlocked("存在未完成的连接恢复待办，需人工核对后重启服务")
            drift = self._path_drift_reason()
            if drift:
                self._apply_locked(
                    state=STATE_NEEDS_USER_ACTION,
                    reason="path_invalid",
                    message=drift,
                    binding="not_applicable",
                    session_preserved=None,
                )
                raise ConnectBlocked(drift)
            if self._risk_lock_file() is not None:
                self._apply_locked(
                    state=STATE_NEEDS_USER_ACTION,
                    reason="risk_lock_present",
                    message="风险锁存在（cdp_risk_lock.json）：连接不会清锁、不会重试，"
                            "请人工按既有恢复流程处理后重启服务。",
                    binding="not_applicable",
                    session_preserved=None,
                )
                raise ConnectBlocked("风险锁存在（cdp_risk_lock.json），需人工按既有恢复流程处理")

            task_id = uuid.uuid4().hex
            self._task_id = task_id
            self._deadline = self._clock() + self.login_timeout_s + self.lease_grace_s
            self._apply_locked(
                state=STATE_CONNECTING,
                reason="login_in_progress",
                message="连接中：请在打开的浏览器里完成扫码/平台验证。不可取消，"
                        "等待完成或按既有流程停止服务。",
                binding="pending",
                session_preserved=None,
            )
            self._commit = None
            self._snapshot = None
            self._worker_alive = True  # 【修回 A】从这一刻起占用，直到线程真的退出
            threading.Thread(
                target=self._run_task,
                args=(task_id, bool(confirm_bind)),
                name=f"connect-{task_id[:8]}",
                daemon=True,
            ).start()
            return self._snapshot_locked()

    # -- 任务体 ---------------------------------------------------------- #
    def _run_task(self, task_id: str, confirm_bind: bool) -> None:
        fields: dict[str, Any] | None = None
        try:
            fields = self._attempt(task_id, confirm_bind)
        except Exception as exc:  # noqa: BLE001 - 兜底：绝不让线程带着 connecting 死掉
            fields = self._failure(
                STATE_FAILED,
                f"internal_error:{type(exc).__name__}",
                "连接任务内部异常，已停止；凭据按连接前快照恢复。",
                self._snapshot,
                binding="unverified",
            )
        finally:
            with self._lock:
                try:
                    stale = self._task_id != task_id
                    if stale or fields is None:
                        # 【修回 B · 撤回单点】迟到的旧任务（或异常结束）：
                        # 状态 / reason / 账号绑定一概不动，只把自己可能写下的凭据撤回。
                        # 这里也是唯一撤回点 —— 注入撤掉它，R4/R4b/R9 必须变红。
                        if self._restore_credentials(self._snapshot) is not True:
                            # 【F2.3/R2】回滚结果不可信（False 或抛错）→ 落恢复待办，
                            # 不靠 task_active 冒充活线程
                            self._set_recovery_todo("restore_failed")
                        else:
                            # 【fix3c·缺陷1】迟到/超时分支恢复读回成功后，同样要清本任务备份；
                            # 否则重建 manager 命中孤儿备份判据、把已恢复干净的现场当成
                            # "未完成恢复"永久拒绝业务。清理失败 → 落待办并保持阻断。
                            if self._own_backups_present():
                                try:
                                    self._drop_own_backups()
                                except OSError:
                                    self._set_recovery_todo("backup_cleanup_failed")
                                else:
                                    self._clear_inprogress()
                            else:
                                self._clear_inprogress()
                    else:
                        # 【F2.1】**先提交、后发布**：绑定写不进去就不许出现 connected。
                        # 上一版先 `_apply_locked(connected)` 再写绑定，注入 OSError 时
                        # 状态已是 connected/login_ok/matched 而绑定文件根本不存在。
                        commit_error: str | None = None
                        cleanup_failed = False
                        try:
                            self._commit_effects()
                        except BackupCleanupError as exc:
                            # 绑定已写入，失败的是备份清理 → 准确原因 + 保持阻断
                            cleanup_failed = True
                            commit_error = f"backup_cleanup_failed:{exc}"
                        except OSError as exc:
                            commit_error = f"commit_failed:{type(exc).__name__}"
                        self._task_id = None
                        self._deadline = None
                        if commit_error and cleanup_failed:
                            # 【fix3c·缺陷1】不夸大成"绑定提交失败"，也不假装已清理干净：
                            # 落待办、保持阻断，并明确说明当前是"新绑定 + 旧凭据"并存。
                            self._set_recovery_todo("backup_cleanup_failed")
                            self._apply_locked(
                                state=STATE_NEEDS_USER_ACTION,
                                reason="backup_cleanup_failed",
                                message="账号绑定已写入，但连接前备份未能清理：为避免误用历史恢复材料，"
                                        "连接与业务发送保持阻断；请人工核对 connect 目录后处理。",
                                binding="matched",
                                session_preserved=None,
                            )
                        elif commit_error:
                            restored = self._restore_credentials(self._snapshot)
                            if restored is not True:
                                # 回滚也失败/未知 → 落恢复待办，进程内立刻阻断
                                self._set_recovery_todo("commit_and_restore_failed")
                            self._apply_locked(
                                state=STATE_FAILED,
                                reason=commit_error,
                                message="账号绑定提交失败：连接未建立，凭据已按连接前快照恢复。",
                                binding="not_applicable",
                                session_preserved=None if (
                                    self._snapshot is None or self._snapshot["session"] is None
                                ) else bool(restored),
                            )
                        else:
                            # 【R1】提交成功是“任务记录可以消失”的两种合法结局之一
                            self._clear_inprogress()
                            self._apply_locked(**fields)
                finally:
                    self._worker_alive = False  # 占用只在"线程真的退出"时释放

    def _commit_effects(self) -> None:
        """执行"成功收尾"的副作用（只在 finally、且只在非迟到时调用）。"""
        commit = self._commit or {}
        digest = commit.get("binding_digest")
        if digest:
            self._write_binding(str(digest))
        if commit.get("drop_backup"):
            try:
                self._drop_own_backups()
            except OSError as exc:
                # 【fix3c·缺陷1】不许混进"账号绑定提交失败"：这里绑定已经写成功，
                # 失败的是备份清理；调用方据此给准确原因并保持阻断。
                raise BackupCleanupError(type(exc).__name__) from exc

    def _own_backups_present(self) -> bool:
        """本任务是否真的写过备份（决定收尾时"要不要清"）。"""
        snapshot = self._snapshot or {}
        if snapshot.get("session") is not None and self._backup_file().exists():
            return True
        if snapshot.get("salt") is not None and self._salt_backup_file().exists():
            return True
        return False

    def _drop_own_backups(self) -> None:
        """【复审·缺陷1】清理**本任务**写下的两份备份（session + salt）。

        只删快照里确实存在过的文件对应的备份：连接前没有的凭据本来就没有备份，
        不许多删；无法归属的历史恢复材料一律留着（宁可手动清理，不擅自删）。
        删除失败向上抛 `OSError`，由调用方转成"保持阻断"，不掩盖。
        """
        snapshot = self._snapshot or {}
        if snapshot.get("session") is not None:
            self._backup_file().unlink(missing_ok=True)
        if snapshot.get("salt") is not None:
            self._salt_backup_file().unlink(missing_ok=True)

    def _is_stale(self, task_id: str) -> bool:
        """【修回 B】租约到期/被新任务顶替后，本任务就是"旧任务"，不许再改状态或写绑定。"""
        with self._lock:
            return self._task_id != task_id

    def _attempt(self, task_id: str, confirm_bind: bool) -> dict[str, Any]:
        session = self._session_file()
        # 【F1.3】先快照、再进任何可能初始化 TokenStore 的路径。
        # 快照 / 落盘备份失败 → **login 之前就拒绝**，且一个字节都不动（snapshot=None）。
        try:
            snapshot = self._snapshot_credentials()
        except OSError as exc:
            self._snapshot = None
            self._before = None
            return self._failure(STATE_FAILED, f"snapshot_failed:{type(exc).__name__}",
                                 "无法读取现有登录态：为保证不破坏原凭据，本次连接未开始。",
                                 None)
        self._snapshot = snapshot
        self._before = snapshot["session"]

        # 【R1】先落“任务已开始”的持久记录（含两个文件连接前是否存在），再备份、再登录。
        #   顺序不可颠倒：记录是唯一的跨进程证据，备份只是恢复材料。
        try:
            self._begin_task_record(task_id, snapshot)
        except OSError as exc:
            # 任务记录写不进去 = 登录前的持久准备失败 → **不许进入登录**。
            # 与备份共用同一失败域（工作目录不可写），沿用既有 `snapshot_failed:<类型>` 口径；
            # 文案点明是哪一步，便于人工判断磁盘/权限（不写路径、不写账号）。
            return self._failure(STATE_FAILED, f"snapshot_failed:{type(exc).__name__}",
                                 "无法写入任务开始记录（登录前持久准备失败）：本次连接未开始登录。"
                                 "请修复磁盘/权限后重启服务，不要靠重启消除不确定性。",
                                 snapshot)
        # 备份落盘失败沿用既有契约（`failed` + `snapshot_failed:<类型>`）：此时一个字节都没动
        try:
            self._preserve_snapshot_on_disk(snapshot)
        except OSError as exc:
            return self._failure(STATE_FAILED, f"snapshot_failed:{type(exc).__name__}",
                                 "无法读取/备份现有登录态：为保证不破坏原凭据，本次连接未开始。",
                                 snapshot)

        # 【S2】浏览器必须在 login 之前就绪；归属不明/配置非法一律拒绝，且不清 cookie 重登
        if self.browser is not None:
            from app.services.connect_browser import (  # noqa: PLC0415 - 延迟 import，避免无浏览器依赖时也加载
                BrowserConfigError,
                BrowserLaunchError,
                BrowserOwnershipError,
            )

            try:
                self.browser.ensure()
            except BrowserConfigError as exc:
                return self._failure(STATE_NEEDS_USER_ACTION, f"browser_config:{_safe_code(exc)}",
                                     "专用浏览器配置不合法：未启动浏览器、未开始登录。", snapshot,
                                     binding="not_applicable")
            except BrowserOwnershipError as exc:
                return self._failure(STATE_NEEDS_USER_ACTION, f"browser_owner:{_safe_code(exc)}",
                                     "9222 上的浏览器无法证明属于本项目：拒绝接管、未开始登录。", snapshot,
                                     binding="not_applicable")
            except BrowserLaunchError as exc:
                return self._failure(STATE_FAILED, f"browser_launch:{_safe_code(exc)}",
                                     "专用浏览器启动失败（超时或端口被抢）：未开始登录。", snapshot,
                                     binding="not_applicable")

        try:
            envelope = self.runner.login(
                cred_dir=self.cred_dir,
                budget_dir=self.budget_dir,
                cdp_url=self.cdp_url,
                timeout_s=self.login_timeout_s,
            )
        except TimeoutError:
            return self._failure(STATE_TIMEOUT, "login_timeout",
                                 "登录超时（上游未在限定时间内返回）。", snapshot)
        except Exception as exc:  # noqa: BLE001 - 只出类型名，不出正文
            return self._failure(STATE_FAILED, f"login_error:{type(exc).__name__}",
                                 "登录未完成；请查看服务日志后重试。", snapshot)

        if self._is_stale(task_id):
            # 【修回 B】本任务已被判超时/被顶替：结果一律不采信，也不再打上游
            # （判据 ③④ 的 status / me 一次都不许发）。**不在这里回滚** ——
            # 回滚只由 `_run_task` 的 finally 一处做（单点，才好被注入验证）。
            return None

        if not isinstance(envelope, dict) or envelope.get("ok") is False:
            error = (envelope or {}).get("error") if isinstance(envelope, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            normalized = normalize_error_code(str(code)) if code else "UPSTREAM_ERROR"
            if normalized == "RISK_LOCKED":
                return self._failure(STATE_NEEDS_USER_ACTION, "upstream_risk_locked",
                                     "上游返回风控限制：已停止并恢复连接前凭据，"
                                     "需人工按既有恢复流程处理（不自动重试）。",
                                     snapshot)
            if "TIMEOUT" in _safe_code(code) or normalized == "UNCONFIRMED":
                return self._failure(STATE_TIMEOUT, "login_timeout",
                                     "登录超时/未确认（上游未给出可靠结果）。"
                                     "已按连接前快照恢复（以读回核验为准）。", snapshot)
            if _safe_code(code) in {"SESSION_STALE_MANUAL", "CDP_SESSION_STALE_MANUAL"}:
                # 【fix3c·缺陷2】受控同步判定浏览器会话失效 → 停下等人工（不清 cookie、不重登）
                return self._failure(STATE_NEEDS_USER_ACTION, "session_stale_manual",
                                     "浏览器里复用的登录态已失效：已停止，不自动清 Cookie 重登，"
                                     "请在专用浏览器里重新扫码后重试。", snapshot)
            if normalized == "AUTH":
                return self._failure(STATE_FAILED, "auth_rejected",
                                     "上游拒绝了本次登录（认证失败）。"
                                     "已按连接前快照恢复（以读回核验为准）。", snapshot)
            return self._failure(STATE_FAILED, f"upstream_error:{_safe_code(code)}",
                                 "登录未完成。已按连接前快照恢复（以读回核验为准）。", snapshot)

        # 判据 2：凭据必须真的落盘且非空（"文件存在"不够，见模块 docstring）
        if not (session.exists() and session.stat().st_size > 0):
            return self._failure(STATE_FAILED, "session_incomplete",
                                 "登录态未落盘（同步未完成），已恢复连接前凭据。", snapshot)

        # 判据 3：本地登录态可确认（只用来否证，不单独算通过）
        try:
            local = self.runner.local_status(cred_dir=self.cred_dir)
        except Exception:  # noqa: BLE001
            local = None
        if not self._logged_in(local):
            return self._failure(STATE_FAILED, "session_unverified",
                                 "无法确认登录态有效（有文件 ≠ 已登录），已停止并恢复连接前凭据。", snapshot)

        # 判据 4：账号身份核验（fail-closed）
        try:
            source = self.runner.identity_source(cred_dir=self.cred_dir)
        except Exception:  # noqa: BLE001
            source = None
        if not source:
            # 【修回 C】无法确认身份 → 恢复连接前凭据（本次登录的凭据不许留给业务用）
            return self._failure(STATE_NEEDS_USER_ACTION, "account_identity_unverified",
                                 "登录态已同步但无法确认账号身份：已停止并恢复连接前凭据，"
                                 "不会标记为当前账号，请人工核对。",
                                 snapshot, binding="unverified")

        # 【R2】顺序硬性要求：先验绑定文件与**既有**密钥，**最后**才算摘要。
        #   上一版先 `_digest`（内部 `_secret()` 会 `_atomic_write(secret.key)`），
        #   于是"旧绑定缺密钥"在检查前就被补成"新密钥已存在"，自动重建契约被绕过。
        binding_state, stored = self._binding_state()
        secret_state = self._secret_state()
        if binding_state == "corrupt":
            # 【F2.6】坏绑定/版本不符 ≠ 未绑定：不许 confirm_bind 覆盖，也不自动重建密钥
            return self._failure(STATE_NEEDS_USER_ACTION, "binding_corrupt",
                                 "本地账号绑定文件损坏、不可读或版本不符：已停止，不会覆盖旧绑定、"
                                 "不自动重建密钥；请按人工流程核对后处理。",
                                 snapshot, binding="unverified")
        if stored is not None and secret_state != "ok":
            return self._failure(STATE_NEEDS_USER_ACTION, "binding_secret_invalid",
                                 "账号绑定存在但本机密钥缺失或非法：已停止，不自动重建密钥再绑定。",
                                 snapshot, binding="unverified")
        if stored is None and secret_state == "invalid":
            # 没有绑定但密钥文件非法（空/过短）：不许覆盖重建
            return self._failure(STATE_NEEDS_USER_ACTION, "binding_secret_invalid",
                                 "本机绑定密钥文件非法：已停止，不覆盖、不重建；请按人工流程核对。",
                                 snapshot, binding="unverified")
        if stored is None:
            if not confirm_bind:
                # 【修回 C】首次绑定未确认 → 恢复连接前凭据（不留新登录态）
                return self._failure(STATE_NEEDS_USER_ACTION, "account_binding_required",
                                     "首次连接需要你确认把该账号绑定为当前账号："
                                     "勾选确认后再点一次（本次登录态已撤回）。",
                                     snapshot, binding="unverified")
            # 首次绑定 = 唯一允许创建本机密钥的场景
            new_binding: str | None = self._digest(str(source), create=True)
            if not new_binding:  # pragma: no cover - 创建失败时兜底
                return self._failure(STATE_NEEDS_USER_ACTION, "binding_secret_invalid",
                                     "无法创建本机绑定密钥：已停止，未改动任何凭据。",
                                     snapshot, binding="unverified")
        else:
            new_binding = None
            # 既有绑定：只读既有密钥算摘要，**绝不**创建
            comparable = self._digest(str(source), create=False)
            if not comparable or not hmac.compare_digest(stored, comparable):
                # 【修回 C】账号不匹配 → 恢复连接前凭据，且不覆盖旧绑定、不自动修复
                return self._failure(STATE_NEEDS_USER_ACTION, "account_mismatch",
                                     "现存绑定的账号与本次登录的账号不一致：已停止并恢复连接前凭据，"
                                     "不覆盖旧绑定、不自动修复。",
                                     snapshot, binding="mismatch")

        # 【修回 B】写绑定 / 删备份都不在这里做：登记到 commit 槽，由 finally 单点执行
        self._commit = {"binding_digest": new_binding, "drop_backup": True}
        return {
            "state": STATE_CONNECTED,
            "reason": "login_ok",
            "message": "已连接：上游登录态同步完成且账号绑定一致。"
                       "连接成功不代表发送授权，业务发送仍按既有闸门与额度执行。",
            "binding": "matched",
            "session_preserved": None,
        }

    @staticmethod
    def _logged_in(local: Any) -> bool:
        if not isinstance(local, dict) or local.get("ok") is False:
            return False
        data = local.get("data")
        if isinstance(data, dict):
            return bool(data.get("logged_in"))
        return bool(local.get("logged_in"))

    def _failure(self, state: str, reason: str, message: str,
                 snapshot: dict[str, bytes | None] | None, *,
                 binding: str = "not_applicable") -> dict[str, Any]:
        """统一收尾：**一律**恢复连接前凭据，并把结果算成可断言的值。

        【修回 C】失败路径不再有"跳过回滚"的分支：身份未确认 / 首次未确认 /
        不匹配 / 超时 / 上游错误，全部回到连接前状态。
        `session_preserved`：连接前有旧凭据 → 是否字节级还原（True/False）；
        连接前没有 → None（判据是"新写的 `session.enc` 必须被删掉"）。
        """
        restored = self._restore_credentials(snapshot)
        # 【R2】判据从 `is False` 改成 `is not True`：`None`（没有可用快照、一个字节没动）
        #   不算恢复失败；只有"有快照却没能读回核验通过"才落恢复待办。
        if snapshot is not None and restored is not True:
            self._set_recovery_todo("restore_failed")
        elif snapshot is not None:
            # 【复审·缺陷1】恢复读回成功后同样要清掉本任务的两份备份，否则重建 manager
            # 会命中孤儿备份判据、把已恢复干净的现场误判成"未完成恢复"而永久拒绝业务。
            # 清理失败 → 落恢复待办并保持阻断（不把"没清掉"说成"已清理"）。
            # 注意：只有**真正写过备份**的现场才需要清理；连备份都没写成的
            # （`snapshot_failed` 早退）不该被这里再改写成 cleanup 错误。
            if self._own_backups_present():
                try:
                    self._drop_own_backups()
                except OSError:
                    self._set_recovery_todo("backup_cleanup_failed")
                    return {
                        "state": STATE_NEEDS_USER_ACTION,
                        "reason": "backup_cleanup_failed",
                        "message": "连接前备份未能清理：为免误用历史恢复材料，连接与业务发送保持阻断，"
                                   "请人工核对 connect 目录后处理。",
                        "binding": binding,
                        "session_preserved": None if snapshot["session"] is None else True,
                    }
            self._clear_inprogress()
        preserved: bool | None = None if (snapshot is None or snapshot["session"] is None) else bool(restored)
        return {
            "state": state,
            "reason": reason,
            "message": message,
            "binding": binding,
            "session_preserved": preserved,
        }

    def _snapshot_credentials(self) -> dict[str, bytes | None]:
        """连接前快照：`auth/session.enc` + `auth/salt` 的**原始字节**。

        【F1.3】读取异常必须向上抛（调用方在 login 之前拒绝），
        绝不许把"读不出来"当成"原来不存在"，否则后面就会删掉真凭据。
        """
        def _read(path: Path) -> bytes | None:
            return path.read_bytes() if path.exists() else None

        return {"session": _read(self._session_file()), "salt": _read(self._salt_file())}

    def _preserve_snapshot_on_disk(self, snapshot: dict[str, bytes | None]) -> None:
        """快照落一份到工作目录（进程被打断也有据可回滚）。失败向上抛。"""
        if snapshot["session"] is not None:
            _atomic_write(self._backup_file(), snapshot["session"])
        if snapshot["salt"] is not None:
            _atomic_write(self._salt_backup_file(), snapshot["salt"])

    @staticmethod
    def _restore_one(path: Path, before: bytes | None) -> bool:
        """单文件恢复并**读回核验**：`before=None` 表示连接前不存在 → 删除本次新建的。

        【R2】读写 `OSError` **一律不许上抛**：恢复是安全兜底，抛出去会把
        `finally` 里的阻断逻辑整段跳过（复审 R2 的原始缺陷）。失败就返回 `False`，
        由调用方落恢复待办并保持阻断。
        """
        try:
            if before is None:
                if path.exists():
                    path.unlink()
                return not path.exists()
            if path.exists() and path.read_bytes() == before:
                return True
            _atomic_write(path, before)
            return path.exists() and path.read_bytes() == before
        except OSError:
            return False

    def _restore_credentials(self, snapshot: dict[str, bytes | None] | None) -> bool | None:
        """恢复到"连接前"的凭据状态（session **和** salt 一起，按真实路径）。

        返回 `None` = 没有可用快照（一个字节都不动）；`True/False` = 恢复是否读回核验通过。
        【R2】两个文件**各自独立**执行：一个失败不跳过另一个（salt/session 必须同时回到原位，
        否则半恢复状态比不恢复更危险）。
        """
        if snapshot is None:
            return None
        session_ok = self._restore_safe(self._session_file(), snapshot["session"])
        salt_ok = self._restore_safe(self._salt_file(), snapshot["salt"])
        return bool(session_ok and salt_ok)

    def _restore_safe(self, path: Path, before: bytes | None) -> bool:
        """再包一层：`_restore_one` 被替换/底层抛任何异常，都必须落成 `False` 而不是穿出去。

        【R2】`_failure` 与 `_run_task` 的 finally 都在恢复之后才写待办；一旦这里抛出，
        `_set_recovery_todo` 就被跳过、`_worker_alive` 也不清 —— 正是"恢复抛异常不可靠阻断"。
        """
        try:
            return bool(self._restore_one(path, before))
        except Exception:  # noqa: BLE001 - 兜底：任何异常都算恢复失败
            return False

    # -- 账号绑定摘要 ---------------------------------------------------- #
    def _secret_key(self, *, create: bool) -> bytes | None:
        """读本机绑定密钥。**`create=False` 时绝不创建**（R2：检查前不许补密钥）。"""
        path = self._work_dir() / _SECRET_FILE
        if path.exists():
            try:
                return path.read_bytes()
            except OSError:
                return None
        if not create:
            return None
        key = secrets.token_bytes(32)
        _atomic_write(path, key)
        return key

    def _secret(self) -> bytes:
        key = self._secret_key(create=True)
        if key is None:  # pragma: no cover - 写成功即非 None
            raise OSError("secret_key_unavailable")
        return key

    def _digest(self, source: str, *, create: bool = False) -> str | None:
        """本地 HMAC 摘要。**默认不创建密钥** —— 旧绑定缺密钥必须被拒绝，不能先补一把新的。"""
        key = self._secret_key(create=create)
        if key is None:
            return None
        return hmac.new(key, source.encode("utf-8"), hashlib.sha256).hexdigest()

    def _binding_state(self) -> tuple[str, str | None]:
        """绑定文件三态（F2.6）：`absent` 确实未绑定 / `ok` 正常 / `corrupt` 损坏或不可读。

        上一版把"损坏"和"不存在"都归 `None`，于是 `confirm_bind=True` 会直接覆盖坏绑定。
        """
        path = self._binding_file()
        if not path.exists():
            return "absent", None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "corrupt", None
        digest = payload.get("digest") if isinstance(payload, dict) else None
        if not digest or not isinstance(digest, str):
            return "corrupt", None
        # 【R2】严格校验绑定版本与摘要形态：旧版本/异形摘要不许被当成"已绑定"，
        #   否则会静默重绑（迁移必须先列方案，不能就地猜）。
        version = payload.get("v") if isinstance(payload, dict) else None
        if version != 1:
            return "corrupt", None
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest.lower()):
            return "corrupt", None
        return "ok", digest

    def _read_binding(self) -> str | None:  # 兼容旧调用面
        state, digest = self._binding_state()
        return digest if state == "ok" else None

    def _secret_state(self) -> str:
        """密钥三态：`absent` / `ok` / `invalid`（空、过短）。缺失**不自动重建**。"""
        path = self._work_dir() / _SECRET_FILE
        if not path.exists():
            return "absent"
        try:
            data = path.read_bytes()
        except OSError:
            return "invalid"
        return "ok" if len(data) >= 16 else "invalid"

    def _write_binding(self, digest: str) -> None:
        payload = {"v": 1, "digest": digest, "created_at": self._stamp()}
        _atomic_write(self._binding_file(), json.dumps(payload).encode("utf-8"))


# --------------------------------------------------------------------------- #
# 装配 + 路由
# --------------------------------------------------------------------------- #
def resolve_paths(cfg: dict[str, Any]) -> tuple[Path, Path, str | None]:
    """算凭据根 / 共享预算根，并做**唯一有效路径**校验（F4）。

    返回 `(cred_dir, budget_dir, problem)`；`problem` 非 None 时拒绝连接与 boss 业务。
    `fake` 模式不读真实目录、不要求任何凭据 env（保持原离线语义，F4.5）。
    """
    options = (cfg or {}).get("adapter_options") or {}
    adapter_name = (os.environ.get("ADAPTER") or str((cfg or {}).get("adapter", "fake"))).strip().lower()
    cred_raw = (os.environ.get("JOBPILOT_BOSS_CRED_DIR") or "").strip()
    budget_raw = (os.environ.get("JOBPILOT_BOSS_BUDGET_DIR") or "").strip()
    adapter_dir_raw = str(options.get("data_dir") or "~/.boss-agent-test/")

    cred_dir = Path(cred_raw or budget_raw or adapter_dir_raw).expanduser()
    budget_dir = Path(budget_raw or adapter_dir_raw).expanduser()

    if adapter_name != "boss":
        return cred_dir, budget_dir, None

    # ---- boss 模式：显式、绝对、与适配器实际 data_dir 一致 ----
    if not cred_raw:
        return cred_dir, budget_dir, "cred_env_missing"
    if not Path(cred_raw).expanduser().is_absolute():
        return cred_dir, budget_dir, "cred_env_relative"
    if _norm_path(Path(cred_raw).expanduser()) != _norm_path(Path(adapter_dir_raw).expanduser()):
        return cred_dir, budget_dir, "cred_env_conflict_with_adapter"
    if not budget_raw:
        return cred_dir, budget_dir, "budget_env_missing"
    if not Path(budget_raw).expanduser().is_absolute():
        return cred_dir, budget_dir, "budget_env_relative"
    # ---- 真实预算按上游规则解析后必须与期望一致 ----
    try:
        effective = upstream_budget_dir(Path(cred_raw).expanduser())
    except BudgetConfigError as exc:
        return cred_dir, budget_dir, f"budget_config_{type(exc).__name__}"
    if _norm_path(effective) != _norm_path(Path(budget_raw).expanduser()):
        return cred_dir, budget_dir, "budget_mismatch_with_upstream"
    return cred_dir, budget_dir, None


def build_manager(cfg: dict[str, Any], db_file: str | Path, runner: Any | None = None,
                  browser: Any | None = None) -> ConnectManager:
    """从启动配置/环境变量算路径并建管理器。

    - 凭据根 = `JOBPILOT_BOSS_CRED_DIR`（= CLI 的 `--data-dir`，真实凭据在 `<根>/auth/`）
    - 共享预算/风险锁根 = `JOBPILOT_BOSS_BUDGET_DIR`
    - 工作目录 = 数据库所在目录的 `connect/` 子目录（不写凭据目录、不进仓库）
    - 【R4】专用浏览器只由 `main.py` 装配后传入；缺配置时传 `UnavailableBrowser`，
      页面照常可用、连接安全拒绝（不在这里 raise，避免拖垮整个服务）
    `boss` 模式下三者任何不一致 → `path_problem` 非空 → 连接与业务一致拒绝。
    """
    cred_dir, budget_dir, problem = resolve_paths(cfg)
    adapter_name = (os.environ.get("ADAPTER") or str((cfg or {}).get("adapter", "fake"))).strip().lower()
    cdp_url = os.environ.get("JOBPILOT_CDP_URL") or "http://127.0.0.1:9222"
    timeout_s = int(os.environ.get("JOBPILOT_CONNECT_TIMEOUT_S") or 120)
    return ConnectManager(
        data_dir=Path(db_file).parent,
        cred_dir=cred_dir,
        budget_dir=budget_dir,
        runner=runner,
        cdp_url=cdp_url,
        login_timeout_s=timeout_s,
        path_problem=problem,
        strict_paths=(adapter_name == "boss"),
        browser=browser,
    )


def _manager(request: Request) -> ConnectManager:
    return request.app.state.connect


@router.get("/api/connect/status")
def api_connect_status(request: Request) -> dict[str, Any]:
    """连接状态：只有状态、可读原因、脱敏时间。无 token/cookie/路径/账号指纹。"""
    _require_local(request)
    return _manager(request).status()


@router.post("/api/connect/start")
def api_connect_start(request: Request, body: ConnectStartBody | None = None) -> dict[str, Any]:
    """发起连接（单任务互斥）。不发送、不搜索、不清锁、不重试。"""
    _require_local(request)
    try:
        return _manager(request).start(confirm_bind=bool(body.confirm_bind) if body else False)
    except ConnectConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConnectBlocked as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
