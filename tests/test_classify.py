"""Format-A classifier and USCI watermark glue."""
from license_trans.classify import NotFormatA, classify_format_a
from license_trans.ocr import OcrLine
from license_trans.parse import parse_license
from license_trans.usci import checksum_ok, repair_usci


USCI = "91361125MA7ABH19BJ"


def test_repair_g_as_nine():
    assert checksum_ok(USCI)
    assert repair_usci("G1361125MA7ABH19BJ") == USCI


def test_watermark_glue_does_not_beat_real_91_usci():
    """株洲浩航: overlapping GL91… window + checksum repair stole 91430211…"""
    real = "91430211MA7MWFUJ1U"
    lines = [
        OcrLine("营业执照", 0.9, 300, 100, 720, 196),
        OcrLine("统一社会信用代码", 0.9, 51, 110, 218, 133),
        OcrLine("9HO21MAMUUDGL", 0.4, 50, 134, 300, 180),
        OcrLine(real, 0.95, 51, 140, 218, 158),
        OcrLine("DG", 0.4, 217, 149, 286, 175),
        OcrLine("名称 株洲市浩航商贸有限公司", 0.9, 64, 234, 358, 256),
        OcrLine("类型 有限责任公司(自然人投资或控股)", 0.9, 60, 259, 413, 281),
        OcrLine("法定代表人 李晖", 0.9, 55, 287, 206, 314),
    ]
    fields = parse_license(lines)
    assert fields.credit_code == real
    assert checksum_ok(fields.credit_code)


def test_g_prefix_checksum_window_loses_to_91():
    from license_trans.parse import pick_best_usci

    real = "91430211MA7MWFUJ1U"
    blob = "9HO21MAMUUDGL" + real + "DG"
    assert pick_best_usci([blob]) == real


def test_parse_strips_scjdgl_glued_to_usci():
    lines = [
        OcrLine("营业执照", 0.9, 0, 0, 80, 20),
        OcrLine("统一社会信用代码 SCJDGL" + USCI, 0.9, 0, 30, 400, 50),
        OcrLine("名称 横峰县育民建筑劳务服务有限公司", 0.9, 0, 60, 400, 80),
        OcrLine("类型 有限责任公司（自然人独资）", 0.9, 0, 90, 300, 110),
        OcrLine("法定代表人 尹益高", 0.9, 0, 120, 250, 140),
    ]
    fields = parse_license(lines)
    assert fields.credit_code == USCI
    classify_format_a(fields, "".join(ln.text for ln in lines))


def test_company_with_g_prefix_usci_is_still_format_a():
    from license_trans.parse import LicenseFields
    fields = LicenseFields(
        name="横峰县育民建筑劳务服务有限公司",
        type="有限责任公司（自然人独资）",
        credit_code="G1361125MA7ABH19BJ",
        legal_person="尹益高",
    )
    blob = "营业执照法定代表人有限责任公司"
    classify_format_a(fields, blob)
    assert "credit_code_checksum" in fields.warnings


def test_individual_business_still_rejected():
    from license_trans.parse import LicenseFields
    fields = LicenseFields(name="张三", type="个体工商户", credit_code="92361125MA7ABH19BJ")
    try:
        classify_format_a(fields, "个体工商户经营者张三")
        raise AssertionError("expected NotFormatA")
    except NotFormatA:
        pass
