from __future__ import annotations

from .usci import is_company_usci

NON_A_TYPES = (
    "个体工商户",
    "个人独资企业",
    "普通合伙企业",
    "有限合伙企业",
    "特殊普通合伙企业",
    "合伙企业",
    "农民专业合作社",
    "农民专业合作社分支机构",
    "分公司",
)

NON_A_LABELS = ("经营者", "执行事务合伙人", "投资人", "字号", "隶属企业")


class NotFormatA(Exception):
    """Raised when the document is not a Format-A company business license."""


def classify_format_a(fields, blob: str = "") -> None:
    code = (fields.credit_code or "").upper().replace(" ", "")
    type_text = (fields.type or "").replace(" ", "")
    name = fields.name or ""
    blob = blob or ""
    looks_company = (
        any(tok in name for tok in ("有限公司", "股份有限公司", "有限责任公司"))
        or any(tok in type_text for tok in ("有限责任公司", "股份有限公司"))
        or ("法定代表人" in blob and "营业执照" in blob and "个体工商户" not in blob)
    )

    if code and len(code) >= 2 and code[0] == "9" and code[1] in {"2", "3"}:
        raise NotFormatA(f"统一社会信用代码 {code[:2]} 不是公司（91），当前工具只做 A 格式公司执照")

    for token in NON_A_TYPES:
        if token in type_text and "有限责任公司" not in type_text and "股份有限公司" not in type_text:
            raise NotFormatA(f"主体类型「{type_text}」不是 A 格式公司执照")
    if name.endswith("分公司") or "分公司" in type_text:
        raise NotFormatA("分公司属于 G 格式，不是 A 格式公司法人执照")

    if "个体工商户" in blob and "有限公司" not in name:
        raise NotFormatA("个体工商户不属于 A 格式")

    if any(label in blob for label in NON_A_LABELS) and "法定代表人" not in blob:
        raise NotFormatA("执照字段不是公司法人 A 格式")

    if code and not is_company_usci(code) and code[0] != "9":
        # G/C/L prefixes are almost always SCJDGL watermark glue or 9→G OCR,
        # not a real non-SAMR registration department.
        if looks_company:
            if "credit_code_checksum" not in fields.warnings:
                fields.warnings.append("credit_code_checksum")
            return
        raise NotFormatA(f"信用代码登记管理部门 {code[0]} 不是市场监管公司执照")
