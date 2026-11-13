"""微信号提取（任务书 §5.4）——纯函数，**保守优先：宁可漏，不可错**。

必须拒绝：

| 不能误抓 | 为什么 |
|---|---|
| `service@company.com` | 邮箱 |
| `400-123-4567` | 客服电话 |
| `13800138000`（无上下文） | 手机号需锚定词才算 |
| `wxid_` 后跟少于 6 位 | 不像真 ID |

锚定词：`微信` / `weixin` / `wechat` / `VX` / `vx` / `加我` / `加个` / `绿泡泡`
**只从 chatmsg 正文里抓，不从 JD 或公司简介里抓。**
"""

from __future__ import annotations

import re

# 锚定词。注意 "vx" 用 IGNORECASE 统一大小写，避免 VX/vx 重复登记
ANCHOR_WORDS: tuple[str, ...] = ("微信", "weixin", "wechat", "VX", "加我", "加个", "绿泡泡")

# 锚定词之后最多看多少个字符（再远就不是同一句话了）
WINDOW_CHARS = 40

# 邮箱整体先抹掉：`service@company.com` 里的 company 绝不能被当成微信号
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_ANCHOR_RE = re.compile("|".join(re.escape(w) for w in ANCHOR_WORDS), re.IGNORECASE)

# 客服/热线号：400-123-4567 这类，出现即视为噪音（抓不到微信号是正确结果）
_SERVICE_PHONE_RE = re.compile(r"(?<!\d)400[-\s]?\d{3}[-\s]?\d{4}(?!\d)")

# 手机号：必须 1[3-9] 开头 11 位，且前后不能紧邻数字（避免从更长的串里截）
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# 标准 wxid_ 形式：后面至少 6 位
_WXID_RE = re.compile(r"wxid_[A-Za-z0-9_\-]{6,}")

# 普通微信号：字母开头，6~20 位
_PLAIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{5,19}")

# 锚定词本身不是微信号
_NOT_A_WECHAT = {"weixin", "wechat", "wxid", "wechatid"}


def _clean(text: str) -> str:
    """抹掉邮箱与服务热线，避免它们污染候选窗口。"""
    cleaned = _EMAIL_RE.sub(" ", text)
    cleaned = _SERVICE_PHONE_RE.sub(" ", cleaned)
    return cleaned


def extract_wechat(text: str) -> str | None:
    """从一段文本里提取微信号。提取不到返回 `None`。

    优先级：`wxid_xxx` > 手机号 > 普通微信号；三者都必须在锚定词之后的窗口内。
    """
    if not text:
        return None
    raw = str(text)
    cleaned = _clean(raw)

    anchors = list(_ANCHOR_RE.finditer(cleaned))
    if not anchors:
        # 没有锚定词 → 一律不抓（无锚定的 13800138000 就是靠这条拒掉的）
        return None

    for anchor in anchors:
        window = cleaned[anchor.end(): anchor.end() + WINDOW_CHARS]

        match = _WXID_RE.search(window)
        if match:
            return match.group(0)

        match = _PHONE_RE.search(window)
        if match:
            return match.group(0)

        for candidate in _PLAIN_RE.finditer(window):
            value = candidate.group(0)
            lowered = value.lower()
            if lowered in _NOT_A_WECHAT:
                continue
            if lowered.startswith("wxid_"):
                # `wxid_` 后跟少于 6 位：不像真 ID，跳过
                if len(value) - len("wxid_") < 6:
                    continue
            return value

    return None
