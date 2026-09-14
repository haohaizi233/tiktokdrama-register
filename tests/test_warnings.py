"""Tests for parse warnings: term, authority, low-confidence."""
from license_trans.ocr import OcrLine
from license_trans.parse import parse_license
from license_trans.translate import translate_fields


def _line(text: str, score: float = 0.95, x0=0, y0=0, x1=200, y1=30) -> OcrLine:
    return OcrLine(text=text, score=score, x0=x0, y0=y0, x1=x1, y1=y1)


def test_term_warning_when_missing():
    """If 营业期限 label is absent, a 'term' warning must be emitted."""
    lines = [
        _line("营业执照"),
        _line("统一社会信用代码 91110108MA01ABCDEF", x0=0, y0=40, x1=400, y1=70),
        _line("名称 北京测试科技有限公司", x0=0, y0=80, x1=300, y1=110),
        _line("类型 有限责任公司", x0=0, y0=120, x1=200, y1=150),
        _line("法定代表人 张三", x0=0, y0=160, x1=200, y1=190),
        _line("经营范围 技术服务", x0=0, y0=200, x1=300, y1=230),
        _line("注册资本 100万元", x0=0, y0=240, x1=200, y1=270),
        _line("成立日期 2020年1月1日", x0=0, y0=280, x1=200, y1=310),
        _line("住所 北京市海淀区", x0=0, y0=320, x1=300, y1=350),
        _line("登记机关 北京市海淀区市场监督管理局", x0=0, y0=360, x1=400, y1=390),
    ]
    fields = parse_license(lines)
    assert "term" in fields.warnings


def test_authority_warning_when_translate_fails():
    """If authority is missing from parse, 'authority' warning must survive
    through translate_fields even when translate_authority returns empty."""
    lines = [
        _line("营业执照"),
        _line("统一社会信用代码 91110108MA01ABCDEF", x0=0, y0=40, x1=400, y1=70),
        _line("名称 测试有限公司", x0=0, y0=80, x1=300, y1=110),
        _line("类型 有限责任公司", x0=0, y0=120, x1=200, y1=150),
        _line("法定代表人 张三", x0=0, y0=160, x1=200, y1=190),
        _line("经营范围 技术服务", x0=0, y0=200, x1=300, y1=230),
        _line("注册资本 100万元", x0=0, y0=240, x1=200, y1=270),
        _line("成立日期 2020年1月1日", x0=0, y0=280, x1=200, y1=310),
        _line("住所 某地", x0=0, y0=320, x1=100, y1=350),
        _line("营业期限 长期", x0=0, y0=400, x1=200, y1=430),
    ]
    fields = parse_license(lines)
    assert fields.authority == ""
    assert "authority" in fields.warnings
    result = translate_fields(fields)
    assert "authority" in result["warnings"]


def test_low_confidence_warning():
    """If a field's OCR lines have score < 0.6, a low-confidence warning is emitted."""
    lines = [
        _line("营业执照"),
        _line("统一社会信用代码 91110108MA01ABCDEF", x0=0, y0=40, x1=400, y1=70),
        _line("名称 北京测试科技有限公司", score=0.3, x0=0, y0=80, x1=300, y1=110),
        _line("类型 有限责任公司", x0=0, y0=120, x1=200, y1=150),
        _line("法定代表人 张三", x0=0, y0=160, x1=200, y1=190),
        _line("经营范围 技术服务", x0=0, y0=200, x1=300, y1=230),
        _line("注册资本 100万元", x0=0, y0=240, x1=200, y1=270),
        _line("成立日期 2020年1月1日", x0=0, y0=280, x1=200, y1=310),
        _line("住所 北京市海淀区", x0=0, y0=320, x1=300, y1=350),
        _line("登记机关 北京市海淀区市场监督管理局", x0=0, y0=360, x1=400, y1=390),
        _line("营业期限 长期", x0=0, y0=400, x1=200, y1=430),
    ]
    fields = parse_license(lines)
    assert "name_low_confidence" in fields.warnings
    # other fields with good scores should NOT have low-confidence warnings
    assert "type_low_confidence" not in fields.warnings


def test_no_low_confidence_when_score_good():
    """All fields with good OCR scores should not trigger low-confidence warnings."""
    lines = [
        _line("营业执照"),
        _line("统一社会信用代码 91110108MA01ABCDEF", x0=0, y0=40, x1=400, y1=70),
        _line("名称 北京测试科技有限公司", score=0.9, x0=0, y0=80, x1=300, y1=110),
        _line("类型 有限责任公司", score=0.9, x0=0, y0=120, x1=200, y1=150),
        _line("法定代表人 张三", score=0.9, x0=0, y0=160, x1=200, y1=190),
        _line("经营范围 技术服务", score=0.9, x0=0, y0=200, x1=300, y1=230),
        _line("注册资本 100万元", score=0.9, x0=0, y0=240, x1=200, y1=270),
        _line("成立日期 2020年1月1日", score=0.9, x0=0, y0=280, x1=200, y1=310),
        _line("住所 北京市海淀区", score=0.9, x0=0, y0=320, x1=300, y1=350),
        _line("登记机关 北京市海淀区市场监督管理局", score=0.9, x0=0, y0=360, x1=400, y1=390),
        _line("营业期限 长期", score=0.9, x0=0, y0=400, x1=200, y1=430),
    ]
    fields = parse_license(lines)
    low_conf = [w for w in fields.warnings if w.endswith("_low_confidence")]
    assert low_conf == []
