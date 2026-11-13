"""配置加载 + 三条必备校验（任务书 §5.1 / §7，R4-1 / N6 / N7）。

这里是**唯一**读配置文件的地方。core 层拿到的是合并好的扁平 dict（`merged_cfg`），
不碰磁盘、不知道自己吃的是哪两个文件。

三条校验：
  1. `quota.daily_greet > quota.daily_greet_max`        → 抛 StartupError（中文），启动即退出
  2. `directions` 的 `share` 之和 != 1.0                → 归一化 + 打 warning，**不崩**
  3. `.env` 缺失 / 没填 Key                             → **降级运行**，`llm_configured: false`，不退出
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_MATCHER_PATH = PROJECT_ROOT / "config" / "matcher.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

# config.yaml 里要展开进 merged_cfg 的两段：core 层只认扁平键
FLATTEN_SECTIONS = ("target", "quota")

#: 合法 `llm.mode` 枚举（P2-C §4.2）。
#: **必须与 `app/adapters/llm_gateway.py::LLM_MODES` 保持一致**——这里刻意重复一份，
#: 因为 config 层不能 import adapters（llm_gateway 反过来要 import 本文件的 StartupError，
#: 真 import 就成了循环依赖）。
LLM_MODES: tuple[str, ...] = ("off", "cache", "batch", "hybrid", "live")

#: `llm.mode` 默认值：不花钱（决策 2）
DEFAULT_LLM_MODE: str = "off"

#: 【P2-B §6.3】单轮 `detail` 条数的**硬上限**。不是审美问题，是从上游
#: **20 次/分钟**的共享请求预算倒推出来的：一轮里 search(3) + detail(N) +
#: greet(3) + 反查 chat(3) ≤ 20，于是 N ≤ 11，取 10 留一次重试余量。
#: 超过 20 就是"这一轮注定把窗口吃穿"，启动即拒绝，不许带着它跑。
DETAIL_BATCH_HARD_LIMIT: int = 20


class StartupError(RuntimeError):
    """启动期配置错误。宁可起不来，也不要带着坏配置跑。"""


# --------------------------------------------------------------------------- #
# 读文件
# --------------------------------------------------------------------------- #
def read_yaml(path: str | Path) -> dict[str, Any]:
    """读一个 yaml 文件，任何异常都转成中文 StartupError。"""
    p = Path(path)
    if not p.exists():
        raise StartupError(f"配置文件不存在：{p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise StartupError(f"配置文件不是合法 YAML：{p}（{exc}）") from exc
    except OSError as exc:
        raise StartupError(f"配置文件读不出来：{p}（{exc}）") from exc
    if data is None:
        raise StartupError(f"配置文件是空的：{p}")
    if not isinstance(data, dict):
        raise StartupError(f"配置文件顶层必须是 mapping（键值对），当前是 {type(data).__name__}：{p}")
    return data


def load_env(path: str | Path) -> dict[str, str]:
    """极简 .env 解析（KEY=VALUE），不引额外依赖。文件不存在返回空 dict，不报错。"""
    p = Path(path)
    env: dict[str, str] = {}
    if not p.exists():
        return env
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


# --------------------------------------------------------------------------- #
# 校验 1：同名键冲突（R4-1 / N6）
# --------------------------------------------------------------------------- #
def merge_config(config_cfg: dict[str, Any], matcher_cfg: dict[str, Any]) -> dict[str, Any]:
    """matcher.yaml 全部内容 + config.yaml 的 target/quota 段，扁平合并成一个 dict。

    同名键冲突 → StartupError，**不静默覆盖**（任务书 §5.1）。
    """
    merged: dict[str, Any] = {}

    for key, value in matcher_cfg.items():
        merged[key] = value

    for section in FLATTEN_SECTIONS:
        block = config_cfg.get(section) or {}
        if not isinstance(block, dict):
            raise StartupError(f"config.yaml 的 {section} 段必须是 mapping（键值对）")
        for key, value in block.items():
            if key in merged:
                raise StartupError(
                    f"配置键冲突：`{key}` 同时出现在 config/matcher.yaml 与 "
                    f"config.yaml 的 `{section}` 段。每项配置只能有一个来源，请删掉其中一处。"
                    f"（known 例子：preferred_employers / expected_salary 只在 matcher.yaml）"
                )
            merged[key] = value

    for key, value in config_cfg.items():
        if key in FLATTEN_SECTIONS:
            continue
        if key in merged:
            raise StartupError(
                f"配置键冲突：`{key}` 同时出现在 config/matcher.yaml 与 config.yaml 顶层。"
                f"请删掉其中一处。"
            )
        merged[key] = value

    # 嵌套副本留着给不需要扁平键的地方看（页面 / 排障）
    for section in FLATTEN_SECTIONS:
        merged[section] = dict(config_cfg.get(section) or {})

    return merged


# --------------------------------------------------------------------------- #
# 校验 2：额度上限
# --------------------------------------------------------------------------- #
def validate_quota(cfg: dict[str, Any]) -> None:
    """`daily_greet` 不得超过 `daily_greet_max`。违反 → StartupError（中文）。"""
    quota = cfg.get("quota") or {}
    if not isinstance(quota, dict):
        raise StartupError("config.yaml 的 quota 段必须是 mapping（键值对）")

    daily = quota.get("daily_greet")
    maximum = quota.get("daily_greet_max")
    if daily is None or maximum is None:
        return

    try:
        daily_num = int(daily)
        max_num = int(maximum)
    except (TypeError, ValueError) as exc:
        raise StartupError(
            f"配置错误：quota.daily_greet={daily!r}、daily_greet_max={maximum!r} 必须是整数"
        ) from exc

    if daily_num < 0:
        raise StartupError(f"配置错误：quota.daily_greet={daily_num} 不能是负数")

    if daily_num > max_num:
        raise StartupError(
            f"配置错误：quota.daily_greet={daily_num} 大于 daily_greet_max={max_num}。"
            f"daily_greet_max 是硬上限、永不突破，请把 daily_greet 调到 {max_num} 以内。"
        )

    # 【P2-B §6.3 / §6.4】detail 两档额度。四条校验，错一条就启动即死：
    #   daily_detail   单日上限（实验期 50，跑通后收回 30）
    #   detail_batch   单轮上限（由 20 次/分钟预算倒推，硬上限 20）
    # 都是"整数、≥0、batch ≤ 20、batch ≤ daily_detail"。
    detail_batch_num = _nonneg_int(quota, "detail_batch")
    daily_detail_num = _nonneg_int(quota, "daily_detail")

    if detail_batch_num is not None and detail_batch_num > DETAIL_BATCH_HARD_LIMIT:
        raise StartupError(
            f"配置错误：quota.detail_batch={detail_batch_num} 超过硬上限 "
            f"{DETAIL_BATCH_HARD_LIMIT}。上游请求预算是 20 次/分钟且跨进程共享，"
            f"一轮里还要留给 search / greet / 反查 chat（合计 9 次），"
            f"detail_batch 超过 {DETAIL_BATCH_HARD_LIMIT} 必然撞 REQUEST_BUDGET_EXHAUSTED。"
        )

    if (
        detail_batch_num is not None
        and daily_detail_num is not None
        and detail_batch_num > daily_detail_num
    ):
        raise StartupError(
            f"配置错误：quota.detail_batch={detail_batch_num} 大于 daily_detail={daily_detail_num}。"
            f"单轮上限比整天上限还大就永远碰不到日额度，等于这一档保险是坏的——"
            f"请把 detail_batch 调到 {daily_detail_num} 以内。"
        )


def _nonneg_int(quota: dict[str, Any], key: str) -> int | None:
    """取一个"必须是非负整数"的额度键。没写 → `None`（不校验）；坏了 → StartupError（中文）。

    **`int()` 一把梭是不够的**：`int(2.5)` 悄悄返回 2，于是 `detail_batch: 2.5`
    会被当成 2 通过校验——用户以为写了 2.5 条，程序按 2 跑，且没有任何提示。
    所以 float 必须额外要求"值是整数"，bool 直接拒绝（`True` 是 1，太容易被写错）。
    """
    raw = quota.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise StartupError(f"配置错误：quota.{key}={raw!r} 必须是整数，不许写布尔值")
    if isinstance(raw, float) and not float(raw).is_integer():
        raise StartupError(f"配置错误：quota.{key}={raw!r} 必须是整数（不能带小数）")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise StartupError(f"配置错误：quota.{key}={raw!r} 必须是整数") from exc
    if value < 0:
        raise StartupError(f"配置错误：quota.{key}={value} 不能是负数")
    return value


# --------------------------------------------------------------------------- #
# 校验 3：share 归一化（不崩，只告警）
# --------------------------------------------------------------------------- #
def normalize_direction_shares(matcher_cfg: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把 `directions` 的 `share` 归一化到**合计 1.0**。

    口径（R4-12 已裁定，两条口径现在同时成立）：
      - `enabled: true` 的方向 share 合计 == 1.0
      - `enabled: false` 的方向 share 必须为 0.0   ← 新加的不变量
      - 于是"全部方向合计"也 == 1.0，本函数按全部方向等比归一化即可
    仓库里的合法值是 0.6 / 0.2 / 0.0 / 0.2 = 1.0，启动不告警；
    0.6 / 0.2 / 0.1 / 0.2 = 1.1 这类非法值才需要归一化。

    > 裁定原文见 `修复文档-Phase1-骨架批.md` §3；取舍记录见 `交付说明.md` §7。

    返回 `(新的 matcher_cfg, warnings)`；**入参不被修改**，也**不抛异常**（任务书 §7 第 2 条）。
    测试用内联 dict 构造 1.1，不拿仓库 yaml 当被测输入（N7）。
    """
    directions = matcher_cfg.get("directions") or {}
    warnings: list[str] = []
    if not isinstance(directions, dict) or not directions:
        return dict(matcher_cfg), warnings

    shares: dict[str, float] = {
        name: float(conf.get("share") or 0.0)
        for name, conf in directions.items()
        if isinstance(conf, dict)
    }
    if not shares:
        return dict(matcher_cfg), warnings

    total = sum(shares.values())
    if abs(total - 1.0) < 1e-9:
        return dict(matcher_cfg), warnings

    new_directions: dict[str, Any] = {}
    if total <= 0:
        even = round(1.0 / len(shares), 6)
        for name, conf in directions.items():
            new_conf = dict(conf)
            if name in shares:
                new_conf["share"] = even
            new_directions[name] = new_conf
        warnings.append(
            f"directions 的 share 合计为 {total}（无有效权重），已均分为每条 {even} 后继续运行"
        )
        return dict(matcher_cfg, directions=new_directions), warnings

    for name, conf in directions.items():
        new_conf = dict(conf)
        if name in shares:
            new_conf["share"] = round(shares[name] / total, 6)
        new_directions[name] = new_conf

    # 抹掉四舍五入的残差，保证归一化后严格合计 1.0
    drift = round(1.0 - sum(float(new_directions[n]["share"]) for n in shares), 6)
    if drift:
        biggest = max(shares, key=lambda n: float(shares[n]))
        new_directions[biggest]["share"] = round(float(new_directions[biggest]["share"]) + drift, 6)

    warnings.append(
        f"directions 的 share 合计为 {round(total, 6)}（应为 1.0），已按比例归一化，程序照常运行"
    )
    return dict(matcher_cfg, directions=new_directions), warnings


# --------------------------------------------------------------------------- #
# 校验 3b：llm.mode 枚举（P2-C §4.3 第 4 条校验）
# --------------------------------------------------------------------------- #
def validate_llm_mode(cfg: dict[str, Any]) -> None:
    """`llm.mode` 必须是五种之一。非法 → StartupError（中文）。

    缺省（没写 `llm.mode`）算 `off`：默认不花钱，这是决策 2 的拍板结论。
    """
    llm = cfg.get("llm")
    if llm is None:
        return
    if not isinstance(llm, dict):
        raise StartupError("config.yaml 的 llm 段必须是 mapping（键值对）")

    mode = llm.get("mode")
    if mode is None:
        normalized_mode = DEFAULT_LLM_MODE
    else:
        normalized_mode = str(mode).strip().lower()
    if normalized_mode not in LLM_MODES:
        raise StartupError(
            f"配置错误：llm.mode={mode!r} 不是合法值。可选：{' / '.join(LLM_MODES)}。"
            f"默认 {DEFAULT_LLM_MODE}（不联网、零 token）。"
        )

    # 【P2-C F1 守卫】API Key 只许放 `.env`。写进 config.yaml 会随仓库/交付包一起泄露。
    raw_key = llm.get("api_key")
    if raw_key is not None and str(raw_key).strip():
        raise StartupError(
            "配置错误：config.yaml 的 llm 段里写了 api_key。"
            "API Key 只能放 .env（LLM_API_KEY=...），不许写进 config.yaml——"
            "这个文件会进仓库和交付包。请删掉 llm.api_key 并把 Key 移到 .env。"
        )

    cap = llm.get("daily_call_cap")
    if cap is None:
        return
    try:
        cap_num = int(cap)
    except (TypeError, ValueError) as exc:
        raise StartupError(f"配置错误：llm.daily_call_cap={cap!r} 必须是整数") from exc
    if cap_num < 0:
        raise StartupError(f"配置错误：llm.daily_call_cap={cap_num} 不能是负数")


# --------------------------------------------------------------------------- #
# 总入口
# --------------------------------------------------------------------------- #
def load_config(
    config_path: str | Path | None = None,
    matcher_path: str | Path | None = None,
    env_path: str | Path | None = None,
) -> dict[str, Any]:
    """读两个文件，合并成一个 dict（`merged_cfg`），并跑完三条校验。

    返回的 dict 里：
      - matcher.yaml 的全部键（扁平）
      - config.yaml `target` / `quota` 段的键（扁平）
      - `adapter` / `schedule` / `gate` / `llm` / `server` 等顶层段
      - 额外运行期键：`llm_configured` / `warnings` / `config_path` / `matcher_path`
    """
    cfg_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    config_cfg = read_yaml(cfg_path)

    if matcher_path:
        m_path = Path(matcher_path)
    else:
        declared = ((config_cfg.get("target") or {}).get("matcher_config") or "").strip()
        m_path = Path(declared) if declared else DEFAULT_MATCHER_PATH
        if not m_path.is_absolute():
            m_path = PROJECT_ROOT / m_path
    matcher_cfg = read_yaml(m_path)

    # ① 同名键冲突 → 直接抛
    merged = merge_config(config_cfg, matcher_cfg)

    # ② 额度上限 → 直接抛
    validate_quota(merged)

    # ③ share 归一化 → 只告警
    matcher_cfg, warnings = normalize_direction_shares(matcher_cfg)
    merged["directions"] = matcher_cfg.get("directions", {})

    # ③b 【P2-C】`llm.mode` 枚举 → 非法直接抛（默认 off）
    merged["llm"] = dict(config_cfg.get("llm") or {})
    validate_llm_mode(merged)

    # ④ .env 缺失 → 降级，不退出
    env = load_env(Path(env_path) if env_path else DEFAULT_ENV_PATH)
    # 【F8】`.env` 优先，系统环境变量兜底。
    # 原来只读 .env 文件，环境变量里注了 Key 也判成"未配置"；
    # 现在两处都认，所以没人调用的 `load_dotenv_into_os_environ()` 已删除（不留死代码）。
    api_key = (env.get("LLM_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()
    llm_configured = bool(api_key) and not api_key.lower().startswith(("your-", "sk-xxx", "changeme"))
    if not llm_configured:
        warnings.append(
            ".env 缺失或未填 LLM_API_KEY：降级运行（用 FakeLLM / 固定模板），所有接口照常可用"
        )

    merged["llm"]["api_key"] = api_key if llm_configured else ""
    # 【P2-C F1 修复】只写布尔 `llm_configured` 是不够的：`LlmHttpClient` 需要 key 本身，
    # 而 Phase 1 已明确删掉"把 .env 导出到 os.environ"的函数，于是"只写 .env"这条路
    # 会永远回落 `no_api_key`（`llm_configured: true` 与 `last_fallback_reason: no_api_key`
    # 同时出现的自相矛盾状态）。这里把**可用的** key 交给适配器层：
    #   - `llm_configured` 为假（空 / 占位符 your- / sk-xxx / changeme）→ 放空串，
    #     不让 `your-key-here` 这种东西流转到客户端；
    #   - 只放 `merged["llm"]` 里，**不放顶层**（顶层会撞 merge_config 的同名键校验，
    #     也容易被日志/接口整段带出去）；API 响应里不透传 `llm` 段。

    merged["llm_configured"] = llm_configured
    merged["warnings"] = warnings
    merged["config_path"] = str(cfg_path)
    merged["matcher_path"] = str(m_path)

    # 【闭环 3 · 2026-11-12】多账号覆盖：把 `adapter_options.data_dir` 换成当前账号的目录。
    #   只覆盖资源路径（库/上游目录/端口由环境变量与启动器负责），
    #   **不覆盖额度、节奏、目标** —— 那些是业务策略，不该跟着账号漂移。
    #   延迟 import 避免循环依赖（accounts 也要读 cfg）。
    from app.core import accounts as accounts_mod

    merged = accounts_mod.apply_override(merged)
    return merged
