"""_missing_critical must work for both LicenseFields objects and plain dicts."""
from license_trans.parse import LicenseFields
from license_trans.pipeline import _missing_critical


def test_object_with_all_fields():
    f = LicenseFields(name="X公司", credit_code="91110101MA004KQM97", scope="销售",
                      legal_person="张三", capital="100万元")
    assert _missing_critical(f) == []


def test_object_missing_some():
    f = LicenseFields(name="X公司", credit_code="91110101MA004KQM97")
    assert _missing_critical(f) == ["scope", "legal_person", "capital"]


def test_dict_with_all_fields():
    """convert_image 返回 info["fields"] 是 to_dict() 后的 dict —— 回归防护。"""
    f = {
        "name": "X公司",
        "credit_code": "91110101MA004KQM97",
        "scope": "销售",
        "legal_person": "张三",
        "capital": "100万元",
    }
    assert _missing_critical(f) == []


def test_dict_missing_some():
    f = {"name": "X公司", "credit_code": "91110101MA004KQM97", "scope": "销售"}
    assert _missing_critical(f) == ["legal_person", "capital"]


def test_dict_from_real_sidecar_beijing():
    """真实回归: 北京鑫翠 sidecar fields(dict), 仅 name 空。"""
    f = {
        "credit_code": "91110101MA004KQM97",
        "name": "",
        "type": "有限责任公司（自然人独资）",
        "legal_person": "北京市东城区法华寺街136号红桥天雅珠宝市场二层2C-08B",
        "scope": "销售珠宝首饰、修理首饰",
        "capital": "王生武",
        "established": "2016年04月07日",
        "address": "北京市东城区法华寺街136号红桥天雅珠宝市场二层2C-08B",
        "term": "2014年03月28日",
        "authority": "",
        "issue_date": "2014年03月28日",
    }
    assert _missing_critical(f) == ["name"]
