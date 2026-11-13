"""LLM 生成策略网关（任务书 §4 · P2-C）。

**本模块是本批唯一"碰模型"的地方，所以它必须在 `adapters/`**（§1 分层铁律）。
`app/core/composer.py` 一行未改：它对模型的全部依赖只有 `llm.complete(prompt) -> str`，
换策略 = 换注入给 `compose_opener()` 的那个对象。

五种模式（`llm.mode`）：

    off     纯模板拼装，不联网，零 token（**默认值**）
    cache   查 llm_calls.prompt_hash 命中即用，未命中回落模板
    batch   读 data/openers.jsonl 预生成库轮转取用，用完回落模板
    hybrid  缓存 → 预生成 → 真模型，受 llm.daily_call_cap 约束
    live    每次直接调模型

**兜底链恒定**：任何模式下出错 / 超时 / 超预算 / 无 key / 无预生成库 → 退回模板，
流水线不停。但回落**一律留痕**，不做无声降级：

    - 正常模板路径         → llm_calls.model = 'template'
    - 降级回落（有原因）   → llm_calls.model = 'template:fallback'
    - 缓存命中             → llm_calls.model = 'cache'
    - 预生成取用           → llm_calls.model = 'batch'
    - 真模型               → llm_calls.model = 配置里的模型名（如 'deepseek-chat'）

降级原因另存 `LlmGateway.last_fallback_reason`，透出到 `/api/status.llm`。
没有这条，'off' 和 '回落' 两种状态在页面上长得一模一样，审不出来。

## 计费口径（`calls_today`）

`calls_today` / `billable_calls_today` = 今日 `llm_calls` 中
`model NOT IN ('template', 'template:fallback', 'cache', 'batch')` 的行数，
即**真正花钱的模型调用数**。`off` 模式下每岗仍会写一条 `model='template'` 的
行（流水线要可审计），所以 **`llm_calls` 有行 ≠ 花过钱**——判定只看上面这个排除表。
两个键由**同一次查询**得出、一次性赋两个键，永不各自独立计数（否则必然 drift）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from app import models
from app.config import StartupError
from app.core.composer import OPENER_MAX_CHARS, prompt_hash, render

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: 合法 `llm.mode` 枚举。改这个表要同步 `app/config.py` 的第 4 条校验。
LLM_MODES: tuple[str, ...] = ("off", "cache", "batch", "hybrid", "live")

#: 默认每日真模型调用上限（决策 4 已拍板 100）
DEFAULT_DAILY_CALL_CAP: int = 100

#: 这些 model 名**不计费**，也因此**不能**被 cache 命中
#: （模板文案不是模型产出，被命中会把命中率算脏，审计口径就坏了）
NON_BILLABLE_MODELS: tuple[str, ...] = ("template", "template:fallback", "cache", "batch")

MODEL_TEMPLATE: str = "template"
MODEL_FALLBACK: str = "template:fallback"
MODEL_CACHE: str = "cache"
MODEL_BATCH: str = "batch"

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATES_PATH: Path = PROJECT_ROOT / "config" / "prompts" / "opener_templates.yaml"


# --------------------------------------------------------------------------- #
# 预期内的模型异常（**只有这几个能被捕获降级**）
# --------------------------------------------------------------------------- #
class LlmError(Exception):
    """模型调用的基线异常。`reason` 会写进 `last_fallback_reason`。"""

    reason = "llm_error"


class LlmNotConfigured(LlmError):
    """没有 API key / 没配 base_url。启动时就该被 warning 出来。"""

    reason = "no_api_key"


class LlmTimeout(LlmError):
    """超时。"""

    reason = "timeout"


class LlmHTTPError(LlmError):
    """上游 4xx / 5xx / 连接失败。"""

    reason = "http_error"


class LlmBadResponse(LlmError):
    """响应不是合法 JSON、缺字段、或返回空正文。"""

    reason = "bad_response"


#: **只允许**捕获这四类。未知异常必须上抛给 dashboard 的 try/except（会写 runs.error）。
EXPECTED_LLM_ERRORS: tuple[type[BaseException], ...] = (
    LlmNotConfigured,
    LlmTimeout,
    LlmHTTPError,
    LlmBadResponse,
)


# --------------------------------------------------------------------------- #
# 模板资产
# --------------------------------------------------------------------------- #
class TemplateStore:
    """`config/prompts/opener_templates.yaml` 的加载 / 校验 / 确定性选择。

    缺失、为空、文案违禁 → `StartupError`（中文）。**不许静默降级**，
    和 `composer.read_prompt()` 是同一套风格。
    """

    #: 文案里出现这些词 → 直接起不来（与 `opener.md` 同一套约束）
    #: 【P2-C F3 修复】补 "年经验" / "年以上"：`opener.md` 禁的是"年限"这个**语义**，
    #: 只列 "工作年限/执业年限" 会漏掉 "3 年经验" "五年以上" 这类不含"年限"二字的写法。
    #: 数字 + "年" 的写法由下面 FORBIDDEN_PATTERNS 的正则兜住。
    FORBIDDEN_WORDS: tuple[str, ...] = (
        "工作年限",
        "执业年限",
        "年经验",
        "年以上",
        "案件数量",
        "执业证号",
        "胜诉率",
        "客户名称",
        "服务过的客户",
    )

    #: 正则补充：任意"N 年" / "N 年以上" / "N年经验"
    FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (re.compile(r"\d+\s*年"),)

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_TEMPLATES_PATH
        self.templates: list[dict[str, Any]] = self._load()
        # 硬约束：本文件声明的 max_chars 不得突破 OPENER_MAX_CHARS
        self.limit = OPENER_MAX_CHARS

    # -- 加载 -------------------------------------------------------------- #
    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            raise StartupError(
                f"缺少模板资产：{self.path}。开场白模板是本项目的核心资产，"
                f"不允许静默降级成空文案——请从仓库恢复 config/prompts/opener_templates.yaml。"
            )
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise StartupError(f"模板资产不是合法 YAML：{self.path}（{exc}）") from exc

        if not isinstance(data, dict):
            raise StartupError(f"模板资产顶层必须是 mapping（键值对）：{self.path}")
        raw = data.get("templates")
        if not isinstance(raw, list) or not raw:
            raise StartupError(
                f"模板资产里没有任何模板（templates 为空）：{self.path}。"
                f"空模板会让开场白生成失去约束，请补内容。"
            )

        declared_max = data.get("max_chars")
        if declared_max is not None:
            try:
                declared = int(declared_max)
            except (TypeError, ValueError) as exc:
                raise StartupError(f"模板资产 max_chars 必须是整数：{self.path}") from exc
            if declared > OPENER_MAX_CHARS:
                raise StartupError(
                    f"模板资产 max_chars={declared} 超过硬上限 {OPENER_MAX_CHARS} 字：{self.path}。"
                    f"开场白必须 ≤ {OPENER_MAX_CHARS} 字，请调小。"
                )

        templates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise StartupError(f"模板资产第 {index} 条不是 mapping：{self.path}")
            template_id = str(item.get("id") or "").strip()
            text = item.get("text")
            if not template_id:
                raise StartupError(f"模板资产第 {index} 条缺少 id：{self.path}")
            if template_id in seen:
                raise StartupError(f"模板资产 id 重复：{template_id}（{self.path}）")
            if not isinstance(text, str) or not text.strip():
                raise StartupError(
                    f"模板 {template_id} 的 text 为空：{self.path}。空文案不许静默放行。"
                )
            for word in self.FORBIDDEN_WORDS:
                if word in text:
                    raise StartupError(
                        f"模板 {template_id} 含违禁词「{word}」：{self.path}。"
                        f"开场白禁止出现年限 / 案件数量 / 执业证号 / 胜诉率 / 客户名称。"
                    )
            for pattern in self.FORBIDDEN_PATTERNS:
                found = pattern.search(text)
                if found:
                    raise StartupError(
                        f"模板 {template_id} 含违禁写法「{found.group(0)}」：{self.path}。"
                        f"开场白禁止出现年限（含「3 年」「五年以上」这类写法）。"
                    )
            words = item.get("match_words") or []
            if not isinstance(words, list):
                raise StartupError(f"模板 {template_id} 的 match_words 必须是列表：{self.path}")
            seen.add(template_id)
            templates.append(
                {
                    "id": template_id,
                    "match_words": [str(word) for word in words if str(word).strip()],
                    "text": text,
                }
            )
        return templates

    # -- 确定性选择 --------------------------------------------------------- #
    def pick(self, job: dict[str, Any] | None) -> dict[str, Any]:
        """挑一条模板。**确定性**：同一个 job_id 永远挑到同一条。

        规则（§4.5，禁止 random / 字典序）：

        1. 命中数 = `match_words` 里出现在 `job.title` 的词数
        2. 命中数为 0 → 只在"兜底组"（`match_words` 为空的那些）里选
        3. 组内按**本文件声明顺序**排列 → 这就是平手时的显式 tie-break
        4. `idx = int(sha256(job_id), 16) % len(group)` 只在组内做变体轮转；
           `len(group) == 1` 时退化为恒等（= 声明顺序在前的那条）
        """
        job = job or {}
        title = str(job.get("title") or "")
        job_id = str(job.get("job_id") or "")

        hits = [
            sum(1 for word in template["match_words"] if word in title)
            for template in self.templates
        ]
        best = max(hits)

        if best == 0:
            # 无命中：兜底组 = match_words 为空的模板（保持声明顺序）
            group = [t for t in self.templates if not t["match_words"]]
            if not group:  # 作者把所有模板都写了 match_words：退化成全部
                group = list(self.templates)
        else:
            # 平手组：保持声明顺序（不许 sorted，不许字典序）
            group = [t for t, hit in zip(self.templates, hits) if hit == best]

        index = int(hashlib.sha256(job_id.encode("utf-8")).hexdigest(), 16) % len(group)
        return group[index]

    def render_text(self, template_text: str, job: dict[str, Any] | None) -> str:
        """把任意一段带 `{title}` 占位符的文案渲染成 ≤ 60 字正文。

        预生成库（`data/openers.jsonl`）里的文案也走这里，所以「同一模板给不同岗位
        用」时岗位标题永远是对的——不需要为每个岗位预先生成一份。
        """
        job = job or {}
        text = render(
            template_text,
            {
                "title": job.get("title") or "",
                "company": job.get("company") or "",
                "city": job.get("city") or "",
                "salary": job.get("salary") or "",
            },
        )
        return text.strip()[: self.limit]

    def render_for(self, job: dict[str, Any] | None) -> str:
        """挑一条并渲染成 ≤ 60 字的正文。"""
        return self.render_text(self.pick(job)["text"], job)


# --------------------------------------------------------------------------- #
# 真模型客户端（**只能在 adapters/**，core 永不 import 它）
# --------------------------------------------------------------------------- #
class LlmHttpClient:
    """极简 OpenAI 兼容 `/chat/completions` 客户端（只用标准库，不引新依赖）。

    只把**预期内**的失败翻译成 `LlmError` 子类；其余异常原样上抛。
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        llm = cfg.get("llm") or {}
        self.base_url = str(llm.get("base_url") or "").rstrip("/")
        self.model = str(llm.get("model") or "deepseek-chat")
        self.temperature = float(llm.get("temperature") or 0.7)
        self.max_tokens = int(llm.get("max_tokens") or 300)
        self.timeout = float(llm.get("timeout_s") or 30)
        # 【P2-C F1 修复】两条路都要认：`load_config()` 放进来的（`.env` / 系统环境变量
        # 都已被它合并过）优先，`os.environ` 兜底（便于测试直接注环境变量）。
        # 注意：不加 `if cfg.get("llm_configured")` 这道闸——`load_config()` 在
        # `llm_configured` 为假时放的是空串，占位符不会流转到这里。
        self.api_key = str(llm.get("api_key") or os.environ.get("LLM_API_KEY") or "").strip()

    def available(self) -> bool:
        return bool(self.api_key and self.base_url)

    def complete(self, prompt: str) -> tuple[str, int, int]:
        """返回 `(正文, tokens_in, tokens_out)`。失败抛 `LlmError` 子类。"""
        if not self.available():
            raise LlmNotConfigured("没有可用的 LLM_API_KEY")

        payload = json.dumps(
            {
                "model": self.model,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise LlmHTTPError(f"上游返回 HTTP {exc.code}") from exc
        except TimeoutError as exc:  # urllib 的超时就是 socket.timeout（3.10+ 即 TimeoutError）
            raise LlmTimeout("上游超时") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise LlmTimeout("上游超时") from exc
            raise LlmHTTPError(f"连接失败：{exc.reason}") from exc

        try:
            data = json.loads(raw)
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            tokens_in = int(usage.get("prompt_tokens") or 0)
            tokens_out = int(usage.get("completion_tokens") or 0)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LlmBadResponse("响应解析失败") from exc

        if not str(text or "").strip():
            raise LlmBadResponse("响应正文为空")
        return str(text), tokens_in, tokens_out


# --------------------------------------------------------------------------- #
# 落库：一次 complete() 只写一行 llm_calls
# --------------------------------------------------------------------------- #
class CallRecorder:
    """写 `llm_calls`。**一次生成只写一行**，避免重复记账把条数算脏。

    只落 `prompt_hash`，不落 prompt 原文（Phase 1 已有约定，不回退）。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def record(
        self,
        *,
        model: str,
        response: str | None,
        prompt_hash_value: str | None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        uid: str | None = None,
        purpose: str = "opener",
    ) -> None:
        conn = models.connect(self.db_path)
        try:
            models.record_llm_call(
                conn,
                purpose=purpose,
                uid=uid,
                model=model,
                prompt_hash=prompt_hash_value,
                response=response,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# Writer：`composer` 只认 `complete(prompt) -> str`
# --------------------------------------------------------------------------- #
class _BoundWriter:
    """所有策略的公共外壳：绑定 gateway + 该岗位，永远返回非空 ≤60 字正文。"""

    def __init__(self, gateway: "LlmGateway", job: dict[str, Any] | None = None) -> None:
        self.gateway = gateway
        self.job = job or {}

    # -- 记账 -------------------------------------------------------------- #
    def _record(
        self,
        model: str,
        text: str,
        prompt: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        self.gateway.recorder.record(
            model=model,
            response=text,
            prompt_hash_value=prompt_hash(prompt),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    # -- 三种落点 ---------------------------------------------------------- #
    def _plain_template(self, prompt: str) -> str:
        """正常模板路径（off / cache 未命中 / 预生成用完）：**不算降级**。"""
        text = self.gateway.store.render_for(self.job)
        self._record(MODEL_TEMPLATE, text, prompt)
        return text

    def _fallback(self, prompt: str, reason: str | None) -> str:
        """降级回落：**必须留痕**（model='template:fallback' + last_fallback_reason）。"""
        reason = reason or "unknown"
        self.gateway.last_fallback_reason = reason
        text = self.gateway.store.render_for(self.job)
        self._record(MODEL_FALLBACK, text, prompt)
        return text

    def complete(self, prompt: str) -> str:  # pragma: no cover - 由子类实现
        raise NotImplementedError


class TemplateWriter(_BoundWriter):
    """`off`：纯模板，不联网，零 token。"""

    def complete(self, prompt: str) -> str:
        return self._plain_template(prompt)


class CacheWriter(_BoundWriter):
    """`cache`：按 `prompt_hash` 查 `llm_calls` 命中即用，未命中回落模板。"""

    #: 模板文案不是模型产出，**不许**被当成缓存命中（否则命中率与审计口径都失真）
    QUERY = """
        SELECT response FROM llm_calls
         WHERE prompt_hash = ? AND purpose = 'opener'
           AND model NOT IN ('template', 'template:fallback')
           AND response IS NOT NULL AND response <> ''
         ORDER BY id DESC LIMIT 1
    """

    def lookup(self, prompt: str) -> str | None:
        conn = models.connect(self.gateway.db_path)
        try:
            row = conn.execute(self.QUERY, (prompt_hash(prompt),)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        text = str(row["response"] or "").strip()
        return text or None

    def complete(self, prompt: str) -> str:
        cached = self.lookup(prompt)
        if cached:
            text = cached[: self.gateway.store.limit]
            self._record(MODEL_CACHE, text, prompt)  # 命中仍记一行，保证可审计
            return text
        return self._plain_template(prompt)  # 未命中：回落模板，不抛异常


class BatchWriter(_BoundWriter):
    """`batch`：读 `data/openers.jsonl` 轮转取用，用完回落模板。

    同一个 `job_id` 一旦分配到某条预生成文案，就**固定复用它**（写进
    `data/pregen_used.json`），保证"同一岗位两次生成结果相同"。
    """

    def complete(self, prompt: str) -> str:
        line = self._take()
        if line is None:
            return self._plain_template(prompt)  # 库空/用尽：正常回落，不算降级
        text = self.gateway.store.render_text(str(line.get("text") or ""), self.job)
        if not text:
            return self._plain_template(prompt)
        self._record(MODEL_BATCH, text, prompt)
        return text

    def _take(self) -> dict[str, Any] | None:
        lines = self.gateway.load_pregen()
        if not lines:
            return None
        job_id = str(self.job.get("job_id") or "")
        state = self.gateway.load_pregen_used()
        assignments: dict[str, str] = state.setdefault("assignments", {})
        used_ids: list[str] = state.setdefault("used_ids", [])

        # 已分配过 → 恒定复用（determinism）
        if job_id in assignments:
            for line in lines:
                if str(line.get("id")) == assignments[job_id]:
                    return line
            return None

        pool = [line for line in lines if str(line.get("id")) not in set(used_ids)]
        if not pool:
            return None  # 用完 → 调用方回落模板

        index = int(hashlib.sha256(job_id.encode("utf-8")).hexdigest(), 16) % len(pool)
        chosen = pool[index]
        assignments[job_id] = str(chosen.get("id"))
        used_ids.append(str(chosen.get("id")))
        self.gateway.save_pregen_used(state)
        return chosen


class LiveWriter(_BoundWriter):
    """`live`：每次直接调模型。

    无 key / 超时 / HTTP 4xx-5xx / 解析失败 → 降级模板并留痕；
    **未知异常一律上抛**，交给 dashboard 既有的 `except` 写 `runs.error`。
    """

    def complete(self, prompt: str) -> str:
        if self.gateway.remaining_budget() <= 0:
            return self._fallback(prompt, "budget_exhausted")

        try:
            text, tokens_in, tokens_out = self.gateway.client.complete(prompt)
        except EXPECTED_LLM_ERRORS as exc:  # 只抓预期内的四类
            return self._fallback(prompt, exc.reason)

        text = str(text or "").strip()
        if not text:
            return self._fallback(prompt, "bad_response")

        text = text[: self.gateway.store.limit]
        self._record(self.gateway.client.model, text, prompt, tokens_in, tokens_out)
        return text


class HybridWriter(_BoundWriter):
    """`hybrid`：缓存 → 预生成 → 真模型（受 daily_call_cap 约束）。"""

    def complete(self, prompt: str) -> str:
        cached = CacheWriter(self.gateway, self.job).lookup(prompt)
        if cached:
            text = cached[: self.gateway.store.limit]
            self._record(MODEL_CACHE, text, prompt)
            return text

        line = BatchWriter(self.gateway, self.job)._take()
        if line is not None:
            text = self.gateway.store.render_text(str(line.get("text") or ""), self.job)
            if text:
                self._record(MODEL_BATCH, text, prompt)
                return text

        return LiveWriter(self.gateway, self.job).complete(prompt)


# --------------------------------------------------------------------------- #
# 网关
# --------------------------------------------------------------------------- #
class LlmGateway:
    """按配置给每个岗位发一个 writer。**不做全局可变状态**（同一网关可并发）。

    唯一的可变字段是 `last_fallback_reason`（诊断用，读多写少）。
    """

    def __init__(
        self,
        cfg: dict[str, Any],
        db_path: str | Path,
        templates_path: str | Path | None = None,
        client: Any | None = None,
    ) -> None:
        self.cfg = cfg or {}
        self.db_path = Path(db_path)
        llm = self.cfg.get("llm") or {}

        self.mode = str(llm.get("mode") or "off").strip().lower()
        if self.mode not in LLM_MODES:
            raise StartupError(
                f"llm.mode={self.mode!r} 不是合法值。可选：{' / '.join(LLM_MODES)}"
                f"（默认 off：不花钱）。"
            )
        try:
            self.daily_call_cap = int(llm.get("daily_call_cap", DEFAULT_DAILY_CALL_CAP))
        except (TypeError, ValueError) as exc:
            raise StartupError(
                f"llm.daily_call_cap={llm.get('daily_call_cap')!r} 必须是整数"
            ) from exc
        if self.daily_call_cap < 0:
            raise StartupError(f"llm.daily_call_cap={self.daily_call_cap} 不能是负数")

        self.store = TemplateStore(templates_path)
        self.client = client if client is not None else LlmHttpClient(self.cfg)
        self.last_fallback_reason: str | None = None

        pregen = llm.get("pregen_path")
        if pregen:
            candidate = Path(str(pregen))
            # 【P2-C F3 修复】相对路径锚定**项目根**，不是当前工作目录：
            # `pregenerate.py` 写的是 `PROJECT_ROOT/data/openers.jsonl`，若从别处起
            # uvicorn（CWD≠项目根），旧写法会去读另一个文件，表现成"库是空的、全回落模板"。
            self.pregen_path = candidate if candidate.is_absolute() else (PROJECT_ROOT / candidate)
        else:
            self.pregen_path = self.db_path.parent / "openers.jsonl"
        self.pregen_used_path = self.pregen_path.parent / "pregen_used.json"
        self.recorder = CallRecorder(self.db_path)

    # -- writer ------------------------------------------------------------ #
    def writer_for(self, job: dict[str, Any] | None = None) -> _BoundWriter:
        """返回**绑定了该岗位**的策略对象。模板策略直接用 job 字段，不解析 prompt。"""
        if self.mode == "cache":
            return CacheWriter(self, job)
        if self.mode == "batch":
            return BatchWriter(self, job)
        if self.mode == "live":
            return LiveWriter(self, job)
        if self.mode == "hybrid":
            return HybridWriter(self, job)
        return TemplateWriter(self, job)

    # -- 预算 -------------------------------------------------------------- #
    def billable_calls_today(self) -> int:
        """今日**真的花过钱**的模型调用数（模板 / 缓存 / 预生成统统不算）。"""
        placeholders = ", ".join("?" for _ in NON_BILLABLE_MODELS)
        conn = models.connect(self.db_path)
        try:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS n FROM llm_calls
                 WHERE purpose = 'opener'
                   AND date(created_at) = date('now')
                   AND model NOT IN ({placeholders})
                """,
                NON_BILLABLE_MODELS,
            ).fetchone()
        finally:
            conn.close()
        return int(row["n"]) if row else 0

    def remaining_budget(self) -> int:
        return max(self.daily_call_cap - self.billable_calls_today(), 0)

    # -- 状态 -------------------------------------------------------------- #
    def stats_today(self) -> dict[str, Any]:
        """`/api/status.llm`。

        结构（§4.8 硬指标）：`{mode, calls_today, daily_cap, remaining, cache_hit}`，
        另加本批新增的 `billable_calls_today`（同值同源）与 `last_fallback_reason`。

        **同值同源**：`calls_today` 与 `billable_calls_today` 来自同一次查询、
        在同一个 dict 字面量里赋两个键，禁止各自独立计数（否则以后必然 drift）。
        """
        # 这一次查询同时算出两个键的值
        billable = self.billable_calls_today()

        conn = models.connect(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM llm_calls
                 WHERE purpose = 'opener' AND model = 'cache'
                   AND date(created_at) = date('now')
                """
            ).fetchone()
        finally:
            conn.close()
        cache_hit = int(row["n"]) if row else 0

        return {
            "mode": self.mode,
            # 键名按 §4.8 保留 calls_today；口径 = 计费调用数（不含 template/fallback/cache/batch）。
            # 将来要改口径：改实现、不改键名。
            "calls_today": billable,
            "billable_calls_today": billable,
            "daily_cap": self.daily_call_cap,
            "remaining": max(self.daily_call_cap - billable, 0),
            "cache_hit": cache_hit,
            "last_fallback_reason": self.last_fallback_reason,
        }

    # -- 预生成库 ---------------------------------------------------------- #
    def load_pregen(self) -> list[dict[str, Any]]:
        """读 `data/openers.jsonl`。缺失/坏行直接跳过（不崩流水线）。"""
        if not self.pregen_path.exists():
            return []
        lines: list[dict[str, Any]] = []
        for raw in self.pregen_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("text"):
                lines.append(item)
        return lines

    def load_pregen_used(self) -> dict[str, Any]:
        """读已用记录。文件可以随手删（删了就等于全部重新可用）。"""
        if not self.pregen_used_path.exists():
            return {"assignments": {}, "used_ids": []}
        try:
            data = json.loads(self.pregen_used_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"assignments": {}, "used_ids": []}
        if not isinstance(data, dict):
            return {"assignments": {}, "used_ids": []}
        data.setdefault("assignments", {})
        data.setdefault("used_ids", [])
        return data

    def save_pregen_used(self, state: dict[str, Any]) -> None:
        self.pregen_used_path.parent.mkdir(parents=True, exist_ok=True)
        self.pregen_used_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
