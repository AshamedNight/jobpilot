"""LLM 话术生成 + FakeLLM（任务书 §5.5）。

硬性约束（N8 / R10）：

- **prompt 正文在 `config/prompts/opener.md`，不在 Python 代码里**。
- 文件不存在或为空 → 抛 `ConfigError`（中文），**不静默用内置默认值**。
  理由：prompt 是本项目核心资产之一，静默降级会让"禁止撒谎"的约束失效。
- 开场白 ≤ 60 字，回复 ≤ 100 字，超长截断。
- `FakeLLM` 不联网、不花钱，测试和无 Key 时都用它。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

OPENER_MAX_CHARS: int = 60
REPLY_MAX_CHARS: int = 100

PROMPT_DIR: Path = Path(__file__).resolve().parents[2] / "config" / "prompts"


class ConfigError(Exception):
    """配置缺失（例如 `prompts/*.md` 不存在或为空）。**不允许静默降级**。"""


class FakeLLM:
    """不联网、不花钱、返回固定文本。测试和无 Key 时都用它。

    `calls` 记录每次收到的 prompt，排障与断言用（真实 LLM 也建议记 `prompt_hash`）。
    """

    def __init__(self, text: str | None = None, max_chars: int | None = None) -> None:
        self.text = text if text is not None else (
            "您好，看到贵司在招法务岗。我在合同审核与合规方面有实操经验，"
            "方便的话想和您聊聊岗位细节。"
        )
        self.max_chars = max_chars
        self.calls: list[str] = []

    def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self.max_chars is not None:
            return self.text[: self.max_chars]
        return self.text

    @property
    def call_count(self) -> int:
        return len(self.calls)


def read_prompt(name: str, prompt_dir: str | Path | None = None) -> str:
    """读一个 prompt 模板。缺失或空 → `ConfigError`（中文）。"""
    directory = Path(prompt_dir) if prompt_dir else PROMPT_DIR
    path = directory / name
    if not path.exists():
        raise ConfigError(
            f"缺少 prompt 模板：{path}。这个文件是本项目的核心资产，"
            f"不允许静默降级成内置默认值——请从仓库恢复 config/prompts/。"
        )
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ConfigError(f"prompt 模板是空的：{path}。空模板会让话术生成失去约束，请补内容。")
    return text


class _SafeValues(dict):
    """缺键时渲染成空串，避免模板里多一个占位符就整条链路崩。"""

    def __missing__(self, key: str) -> str:
        return ""


def render(template: str, values: dict[str, Any]) -> str:
    return template.format_map(_SafeValues(values))


def prompt_hash(prompt: str) -> str:
    """prompt 的短哈希，写 `llm_calls.prompt_hash` 用（不落原文，省空间也避免泄露简历）。"""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def compose_opener(
    job: dict[str, Any],
    resume_summary: str,
    llm: Any,
    prompt_dir: str | Path | None = None,
) -> str:
    """生成开场白。返回文本 **≤ 60 字**（超长截断）。"""
    template = read_prompt("opener.md", prompt_dir)
    prompt = render(
        template,
        {
            "job_title": job.get("title") or "",
            "company": job.get("company") or "",
            "salary": job.get("salary") or "",
            "city": job.get("city") or "",
            "jd_text": job.get("jd_text") or "",
            "resume_summary": resume_summary or "",
        },
    )
    text = (llm.complete(prompt) or "").strip()
    return text[:OPENER_MAX_CHARS]


def compose_reply(
    conversation: dict[str, Any],
    messages: list[dict[str, Any]],
    llm: Any,
    prompt_dir: str | Path | None = None,
) -> str:
    """生成回复。返回文本 **≤ 100 字**（超长截断）。"""
    template = read_prompt("reply.md", prompt_dir)
    recent = "\n".join(
        f"{m.get('direction', '?')}: {m.get('content', '')}" for m in (messages or [])[-5:]
    )
    prompt = render(
        template,
        {
            "hr_name": conversation.get("hr_name") or "",
            "company": conversation.get("company") or "",
            "state": conversation.get("state") or "",
            "messages": recent,
        },
    )
    text = (llm.complete(prompt) or "").strip()
    return text[:REPLY_MAX_CHARS]
