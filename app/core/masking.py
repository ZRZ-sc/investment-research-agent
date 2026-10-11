"""日志脱敏：手机号 / 地址 / token / 身份证 / 邮箱等敏感信息。

需求文档 3.6 节：敏感信息（手机号、地址）需脱敏，禁止记录明文 token。

使用方式：
    from app.core.masking import mask_text, mask_phone, safe_log_payload
    logger.info("query={}", mask_text(question))          # 自由文本按正则脱敏
    logger.info("token={}", mask_token(token))            # 单值脱敏
    logger.info("payload={}", safe_log_payload(payload))  # 递归按字段名脱敏
"""
import re

# ---- 正则 ----
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")                 # 中国大陆手机号
_LANDLINE_RE = re.compile(r"(?<!\d)(0\d{2,3}-?\d{7,8})(?!\d)")       # 固话
_TOKEN_RE = re.compile(
    r"(?i)\b(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+[A-Za-z0-9._\-]{6,}|"
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,})\b"
)
_ID_CARD_RE = re.compile(r"(?<!\d)(\d{17}[\dXx]|\d{15})(?!\d)")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-]{1,64})@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
# 地址：省/市/区/县/街道/路/号/栋/室 等中文地址特征串
_ADDRESS_RE = re.compile(
    r"[\u4e00-\u9fff]{2,10}(?:省|市|自治区)"
    r"[\u4e00-\u9fff]{0,12}(?:市|区|县|旗)"
    r"[\u4e00-\u9fff0-9A-Za-z]{0,20}(?:路|街|道|巷|弄|村|镇|乡)"
    r"[\u4e00-\u9fff0-9A-Za-z]{0,20}(?:号|栋|幢|座|单元|室|楼)"
)
# 兜底：较长地址片段（含省市区但缺少号/室时）
_ADDRESS_LOOSE_RE = re.compile(
    r"[\u4e00-\u9fff]{2,10}(?:省|市)[\u4e00-\u9fff]{2,20}(?:区|县|市)[\u4e00-\u9fff0-9A-Za-z]{2,}"
)

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(token|secret|password|passwd|pwd|api[_-]?key|authorization|phone|mobile|tel|"
    r"address|addr|id[_-]?card|email|name)"
)


def mask_phone(value: str | None) -> str:
    """13812345678 → 138****5678"""
    if not value:
        return ""

    def _sub(m: re.Match) -> str:
        s = m.group(1)
        return f"{s[:3]}****{s[-4:]}" if len(s) == 11 else s[:3] + "****"

    return _PHONE_RE.sub(_sub, str(value))


def mask_token(value: str | None) -> str:
    """sk-abcdef123456 → sk-****3456；Bearer xxx → Bearer ****"""
    if not value:
        return ""
    text = str(value)

    def _sub(m: re.Match) -> str:
        token = m.group(1)
        if token.lower().startswith("bearer"):
            raw = token.split(None, 1)[-1]
            return f"Bearer ****{raw[-4:]}" if len(raw) > 4 else "Bearer ****"
        if len(token) <= 8:
            return "****"
        return f"{token[:3]}****{token[-4:]}"

    return _TOKEN_RE.sub(_sub, text)


def mask_address(value: str | None) -> str:
    """北京市朝阳区建国路88号SOHO现代城3号楼1201室 → 北京市朝阳区建国路88号****"""
    if not value:
        return ""
    text = str(value)
    text = _ADDRESS_RE.sub(lambda m: m.group(0)[:14] + "****", text)
    text = _ADDRESS_LOOSE_RE.sub(lambda m: m.group(0)[:10] + "****", text)
    return text


def mask_email(value: str | None) -> str:
    if not value:
        return ""
    return _EMAIL_RE.sub(lambda m: f"{m.group(1)[:1]}***@{m.group(2)}", str(value))


def mask_id_card(value: str | None) -> str:
    if not value:
        return ""

    def _sub(m: re.Match) -> str:
        s = m.group(1)
        return f"{s[:6]}********{s[-4:]}"

    return _ID_CARD_RE.sub(_sub, str(value))


def mask_text(text: str | None) -> str:
    """对自由文本做多规则脱敏（日志里记录用户问题时使用）。"""
    if not text:
        return ""
    out = str(text)
    out = mask_token(out)
    out = mask_phone(out)
    out = mask_address(out)
    out = mask_email(out)
    out = mask_id_card(out)
    out = _LANDLINE_RE.sub(lambda m: m.group(1)[:4] + "****", out)
    return out


def mask_value(key: str, value):
    """按字段名选择脱敏策略；无敏感特征则原样返回。"""
    if value is None:
        return None
    k = (key or "").lower()
    if not _SENSITIVE_KEY_RE.search(k):
        return value
    if "token" in k or "secret" in k or "password" in k or "pwd" in k or "key" in k or "auth" in k:
        return mask_token(str(value))
    if "phone" in k or "mobile" in k or "tel" in k:
        return mask_phone(str(value))
    if "address" in k or "addr" in k:
        return mask_address(str(value))
    if "email" in k:
        return mask_email(str(value))
    if "id_card" in k:
        return mask_id_card(str(value))
    return mask_text(str(value))


def safe_log_payload(payload, _depth: int = 0):
    """递归脱敏 dict/list，用于打印外部输入（请求体、第三方回调等）。"""
    if _depth > 6:
        return "<max-depth>"
    if isinstance(payload, dict):
        return {k: safe_log_payload(mask_value(k, v), _depth + 1) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [safe_log_payload(v, _depth + 1) for v in payload]
    if isinstance(payload, str):
        return mask_text(payload)
    return payload
