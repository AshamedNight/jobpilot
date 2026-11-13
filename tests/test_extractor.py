"""微信号提取（任务书 §5.4）—— 6 条，含反例。

保守优先：**宁可漏，不可错**。反例和正例一样重要。
"""

from __future__ import annotations

from app.core.extractor import extract_wechat


def test_extract_after_wechat_anchor():
    """① 抓 `微信：abc_123`。"""
    assert extract_wechat("微信：abc_123") == "abc_123"
    # 同一句里带前缀也要正确
    assert extract_wechat("加我微信 abc_123 谢谢") == "abc_123"


def test_extract_plain_wechat_id():
    """② 抓 `我微信 lvshi_zhang888`。"""
    assert extract_wechat("我微信 lvshi_zhang888") == "lvshi_zhang888"
    # 剧本里真实会出现的句子（§5.8 交换成功后对方发的那条）
    assert extract_wechat("我的微信是 lvshi_zhang888") == "lvshi_zhang888"


def test_extract_vx_anchor():
    """③ 抓 `加个VX：zhang_lawyer`（大小写都要认）。"""
    assert extract_wechat("加个VX：zhang_lawyer") == "zhang_lawyer"
    assert extract_wechat("加个vx：zhang_lawyer") == "zhang_lawyer"


def test_reject_email():
    """④ **不抓** `service@company.com`（邮箱）。"""
    assert extract_wechat("service@company.com") is None
    # 邮箱 + 锚定词也不行：邮箱里的域名不能当微信号
    assert extract_wechat("联系邮箱 service@company.com 或微信") is None
    assert extract_wechat("微信：service@company.com") is None


def test_reject_service_phone():
    """⑤ **不抓** `400-123-4567`（客服电话）。"""
    assert extract_wechat("400-123-4567") is None
    assert extract_wechat("客服电话 400-123-4567 微信同号") is None


def test_reject_unanchored_phone_and_short_wxid():
    """⑥ **不抓**无锚定的 `13800138000`；**不抓** `wxid_` 后跟少于 6 位。

    手机号必须靠锚定词才认——否则 JD 里任何一个电话都会被当成微信号，
    那是"宁可错"的方向，本模块明确反着来。
    """
    assert extract_wechat("13800138000") is None
    assert extract_wechat("联系电话 13800138000") is None

    # 过短的 wxid_ 不像真 ID
    assert extract_wechat("微信 wxid_abc") is None
    # 正常长度的 wxid_ 必须能抓（不然就变成"什么都抓不到"了）
    assert extract_wechat("微信号：wxid_lawyer2026") == "wxid_lawyer2026"

    # 锚定后的手机号是可以抓的（这条用来证明上面拒的是"无锚定"，不是"所有手机号"）
    assert extract_wechat("微信 13800138000") == "13800138000"
