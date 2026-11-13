"""离线预生成库生成器（P2-C §4.6）。

    python -m app.tools.pregenerate --mode template --n 50
    python -m app.tools.pregenerate --mode live     --n 50

产物：`data/openers.jsonl`，每行一个 JSON 对象：

    {"id": "pre_0001", "text": "...", "source": "template|live", "created_at": "..."}

两种模式：

- `template`：**不联网、零成本**。轮流用 `config/prompts/opener_templates.yaml`
  里的每条模板，保留 `{title}` 占位符，消费端（`BatchWriter`）按岗位渲染。
- `live`：一次性真调模型（最省钱形态：一次批量、之后零调用）。**没有 API key
  就明确报错退出**，不静默产出空库——空库会让 batch 模式到运行期才发现"怎么全是模板"。

这个文件只被手动调用，服务进程不 import 它。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.adapters.llm_gateway import LlmError, LlmHttpClient, TemplateStore
from app.config import StartupError, load_config
from app.core.composer import OPENER_MAX_CHARS, read_prompt, render

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = PROJECT_ROOT / "data" / "openers.jsonl"
DEFAULT_TEMPLATES = PROJECT_ROOT / "config" / "prompts" / "opener_templates.yaml"

#: live 模式用的岗位素材（预生成文案要通用于任何岗位，所以只填方向性信息）
LIVE_JOB: dict[str, str] = {
    "title": "法务",
    "company": "",
    "salary": "",
    "city": "",
    "jd_text": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_template_lines(store: TemplateStore, n: int) -> list[dict[str, Any]]:
    """轮流取模板，**保留 `{title}` 占位符**（由消费端渲染）。"""
    lines: list[dict[str, Any]] = []
    templates = store.templates
    for index in range(n):
        template = templates[index % len(templates)]
        lines.append(
            {
                "id": f"pre_{index + 1:04d}",
                "text": template["text"],
                "source": "template",
                "created_at": _now(),
            }
        )
    return lines


def _build_live_lines(client: LlmHttpClient, n: int) -> list[dict[str, Any]]:
    """一次性真调模型 n 次。任何预期内失败 → 中文报错并退出（不产出半库）。"""
    if not client.available():
        raise StartupError(
            "预生成 --mode live 需要 LLM_API_KEY：请在 .env 里填 LLM_API_KEY 后重试，"
            "或改用 --mode template（离线、零成本）。"
        )
    template = read_prompt("opener.md")
    prompt = render(
        template,
        {
            "job_title": LIVE_JOB["title"],
            "company": LIVE_JOB["company"],
            "salary": LIVE_JOB["salary"],
            "city": LIVE_JOB["city"],
            "jd_text": LIVE_JOB["jd_text"],
            "resume_summary": "",
        },
    )

    lines: list[dict[str, Any]] = []
    for index in range(n):
        try:
            text, _tokens_in, _tokens_out = client.complete(prompt)
        except LlmError as exc:
            raise StartupError(f"预生成第 {index + 1} 条失败（{exc.reason}）：{exc}") from exc
        lines.append(
            {
                "id": f"pre_{index + 1:04d}",
                "text": str(text).strip()[:OPENER_MAX_CHARS],
                "source": "live",
                "created_at": _now(),
            }
        )
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.pregenerate",
        description="生成 data/openers.jsonl 预生成库（P2-C）",
    )
    parser.add_argument("--mode", choices=("template", "live"), default="template")
    parser.add_argument("--n", type=int, default=50, help="生成条数（默认 50）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出路径，默认 data/openers.jsonl")
    parser.add_argument(
        "--templates", default=str(DEFAULT_TEMPLATES), help="模板资产路径"
    )
    parser.add_argument("--config", default=None, help="config.yaml 路径（live 模式读 base_url/model）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.n <= 0:
        print("--n 必须是正整数", file=sys.stderr)
        return 2

    try:
        if args.mode == "template":
            lines = _build_template_lines(TemplateStore(args.templates), args.n)
        else:
            lines = _build_live_lines(LlmHttpClient(load_config(args.config)), args.n)
    except StartupError as exc:
        print(f"预生成失败：{exc}", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")

    print(f"已写入 {len(lines)} 条 → {out}（mode={args.mode}）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
