"""Tests for date disambiguation, QR validation, and old-format license detection."""
from license_trans.ocr import OcrLine
from license_trans.parse import parse_license, DATE_RE


def _line(text: str, score: float = 0.95, x0=0, y0=0, x1=200, y1=30) -> OcrLine:
    return OcrLine(text=text, score=score, x0=x0, y0=y0, x1=x1, y1=y1)


def test_date_disambiguation_term_dates_excluded():
    """When term contains a date range, those dates should not be
    mis-assigned as established or issue_date."""
    lines = [
        _line("营业执照"),
        _line("统一社会信用代码 91110108MA01ABCDEF", x0=0, y0=40, x1=400, y1=70),
        _line("名称 测试有限公司", x0=0, y0=80, x1=300, y1=110),
        _line("类型 有限责任公司", x0=0, y0=120, x1=200, y1=150),
        _line("法定代表人 张三", x0=0, y0=160, x1=200, y1=190),
        _line("经营范围 技术服务", x0=0, y0=200, x1=300, y1=230),
        _line("注册资本 100万元", x0=0, y0=240, x1=200, y1=270),
        _line("成立日期 2020年1月1日", x0=0, y0=280, x1=200, y1=310),
        _line("住所 北京市海淀区", x0=0, y0=320, x1=300, y1=350),
        _line("营业期限 2020年1月1日至2030年12月31日", x0=0, y0=360, x1=400, y1=390),
        _line("登记机关 北京市海淀区市场监督管理局", x0=0, y0=400, x1=400, y1=430),
        _line("发证日期 2021年6月15日", x0=0, y0=440, x1=200, y1=470),
    ]
    fields = parse_license(lines)
    assert fields.established == "2020年01月01日"
    assert fields.issue_date == "2021年06月15日"
    assert "2020" in fields.term
    assert "2030" in fields.term


def test_issue_date_from_label():
    """Issue date should be parsed from 发证日期 label when present."""
    lines = [
        _line("营业执照"),
        _line("统一社会信用代码 91110108MA01ABCDEF", x0=0, y0=40, x1=400, y1=70),
        _line("名称 测试有限公司", x0=0, y0=80, x1=300, y1=110),
        _line("类型 有限责任公司", x0=0, y0=120, x1=200, y1=150),
        _line("法定代表人 张三", x0=0, y0=160, x1=200, y1=190),
        _line("经营范围 技术服务", x0=0, y0=200, x1=300, y1=230),
        _line("注册资本 100万元", x0=0, y0=240, x1=200, y1=270),
        _line("成立日期 2020年1月1日", x0=0, y0=280, x1=200, y1=310),
        _line("住所 北京市海淀区", x0=0, y0=320, x1=300, y1=350),
        _line("营业期限 长期", x0=0, y0=360, x1=200, y1=390),
        _line("登记机关 北京市海淀区市场监督管理局", x0=0, y0=400, x1=400, y1=430),
        _line("发证日期 2021年6月15日", x0=0, y0=440, x1=200, y1=470),
    ]
    fields = parse_license(lines)
    assert fields.issue_date == "2021年06月15日"
    assert "issue_date" not in fields.warnings


def test_old_format_license_detected():
    """When no USCI but a 15-digit registration number exists, flag old format."""
    lines = [
        _line("营业执照"),
        _line("注册号 110108123456789", x0=0, y0=40, x1=400, y1=70),
        _line("名称 测试有限公司", x0=0, y0=80, x1=300, y1=110),
        _line("类型 有限责任公司", x0=0, y0=120, x1=200, y1=150),
        _line("法定代表人 张三", x0=0, y0=160, x1=200, y1=190),
        _line("经营范围 技术服务", x0=0, y0=200, x1=300, y1=230),
        _line("注册资本 100万元", x0=0, y0=240, x1=200, y1=270),
        _line("成立日期 2010年1月1日", x0=0, y0=280, x1=200, y1=310),
        _line("住所 北京市海淀区", x0=0, y0=320, x1=300, y1=350),
        _line("营业期限 长期", x0=0, y0=360, x1=200, y1=390),
        _line("登记机关 北京市海淀区市场监督管理局", x0=0, y0=400, x1=400, y1=430),
    ]
    fields = parse_license(lines)
    assert "credit_code" in fields.warnings
    assert "old_format_license" in fields.warnings


def test_jingying_changsuo_as_address():
    """经营场所 should be accepted as an address label."""
    lines = [
        _line("营业执照"),
        _line("统一社会信用代码 91110108MA01ABCDEF", x0=0, y0=40, x1=400, y1=70),
        _line("名称 测试有限公司", x0=0, y0=80, x1=300, y1=110),
        _line("类型 有限责任公司", x0=0, y0=120, x1=200, y1=150),
        _line("法定代表人 张三", x0=0, y0=160, x1=200, y1=190),
        _line("经营范围 技术服务", x0=0, y0=200, x1=300, y1=230),
        _line("注册资本 100万元", x0=0, y0=240, x1=200, y1=270),
        _line("成立日期 2020年1月1日", x0=0, y0=280, x1=200, y1=310),
        _line("经营场所 北京市海淀区中关村", x0=0, y0=320, x1=300, y1=350),
        _line("营业期限 长期", x0=0, y0=360, x1=200, y1=390),
        _line("登记机关 北京市海淀区市场监督管理局", x0=0, y0=400, x1=400, y1=430),
    ]
    fields = parse_license(lines)
    assert fields.address
    assert "address" not in fields.warnings


def test_term_glues_long_term_from_nearby_token():
    """OCR often splits 2021年03月24日 and 至长期; both belong to 营业期限."""
    lines = [
        _line("营业执照"),
        _line("统一社会信用代码 91361125MA3ABHY985", x0=0, y0=40, x1=400, y1=70),
        _line("名称 横峰县育民建筑劳务服务有限公司", x0=0, y0=80, x1=400, y1=110),
        _line("类型 有限责任公司（自然人独资）", x0=0, y0=120, x1=300, y1=150),
        _line("法定代表人 尹益高", x0=0, y0=160, x1=200, y1=190),
        _line("经营范围 许可项目：建筑劳务分包", x0=0, y0=200, x1=400, y1=230),
        _line("注册资本 壹佰万元整", x0=0, y0=240, x1=200, y1=270),
        _line("成立日期 2021年03月24日", x0=0, y0=280, x1=250, y1=310),
        _line("住所 江西省上饶市横峰县岑阳镇人民大道259号", x0=0, y0=320, x1=400, y1=350),
        _line("营业期限", x0=0, y0=360, x1=80, y1=390),
        _line("2021年03月24日", x0=90, y0=360, x1=240, y1=390),
        _line("至长期", x0=250, y0=360, x1=330, y1=390),
        _line("登记机关 横峰县市场监督管理局", x0=0, y0=400, x1=400, y1=430),
    ]
    fields = parse_license(lines)
    assert fields.term == "2021年03月24日至长期"
