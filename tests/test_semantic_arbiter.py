"""Pre-PDF semantic arbitration must stop address-as-scope and garbled English."""
from license_trans.pipeline import _arbitration_fail_reason
from license_trans.semantic_arbiter import arbitrate, rule_gate
from license_trans.rule_repair import repair_field_value, scope_text_is_plausible
from license_trans.translate import translate_scope, translate_term


def test_shandong_address_is_not_a_scope():
    leaked = (
        "许可项目所山东省潍坊市诸城市密州街道和平北街清明上河园15-1-007号"
        "方可开展经营活动)"
    )
    assert scope_text_is_plausible(leaked) is False
    assert translate_scope(leaked) == ""


def test_rule_gate_blocks_shandong_garbage():
    fields = {
        "name": "山东放杖溪山商贸有限公司",
        "credit_code": "91370782MA3PPDBR8K",
        "scope": (
            "许可项目所山东省潍坊市诸城市密州街道和平北街清明上河园15-1-007号"
            "方可开展经营活动)"
        ),
        "legal_person": "赵芳",
        "capital": "伍佰万元整",
        "warnings": [],
    }
    english = {
        "name": "Shandong Fangzhangxishan Trading Co., Ltd.",
        "scope": "Licensed items: urban and 15-1-007 carry out business activities",
        "address": "No. 15-1-007, Qingmingshanghe Garden",
        "legal_person": "Zhao Fang",
        "term": "May 7, 2019 to (blank)",
    }
    ruled = rule_gate(fields, english)
    assert ruled.passed is False
    assert "scope" in ruled.field_failures
    msg = _arbitration_fail_reason({"fields": fields, "english": english, "needs_review": []})
    assert "语义仲裁" in msg


def test_rule_gate_passes_real_jewelry_scope():
    fields = {
        "name": "山东放杖溪山商贸有限公司",
        "credit_code": "91370782MA3PPDBR8K",
        "scope": "销售首饰、工艺品、家具、茶叶（依法须经批准的项目，经相关部门批准后方可开展经营活动）",
        "legal_person": "赵芳",
        "capital": "伍佰万元整",
        "type": "有限责任公司(自然人独资)",
        "term": "2019年05月07日至长期",
        "warnings": [],
    }
    english = {
        "name": "Shandong Fangzhangxishan Trading Co., Ltd.",
        "scope": "sales of jewelry, handicrafts, furniture and tea",
        "address": "Shandong",
        "legal_person": "Zhao Fang",
        "term": "May 7, 2019 to Long term",
        "type": "Limited Liability Company (Wholly Owned by a Natural Person)",
    }
    ruled = rule_gate(fields, english)
    assert ruled.passed is True
    assert _arbitration_fail_reason(
        {"fields": fields, "english": english, "needs_review": []}
    ) == ""


def test_ai_fail_blocks_when_rules_pass():
    class FakeAI:
        def is_available(self):
            return True

        def chat_json(self, *a, **k):
            return {
                "decision": "fail",
                "confidence": 0.95,
                "reasons": ["English scope omits jewelry sales"],
                "field_failures": {"scope": "missing items"},
            }

    fields = {
        "name": "山东放杖溪山商贸有限公司",
        "scope": "销售首饰、工艺品、家具",
        "legal_person": "赵芳",
        "capital": "伍佰万元整",
        "credit_code": "91370782MA3PPDBR8K",
        "warnings": [],
    }
    english = {
        "name": "Shandong Fangzhangxishan Trading Co., Ltd.",
        "scope": "sales of jewelry, handicrafts and furniture",
        "legal_person": "Zhao Fang",
    }
    out = arbitrate(fields, english, client=FakeAI())
    assert out.passed is False
    assert out.source == "ai"


def test_blank_term_repairs_to_long_term():
    assert repair_field_value("term", "2019年05月07日至") == (
        "2019年05月07日至长期", "term_blank_end",
    )
    assert repair_field_value("term", "2019年05月07日至 年 月 日") == (
        "2019年05月07日至长期", "term_blank_end",
    )
    assert translate_term("2019年05月07日至") == "May 7, 2019 to Long term"
    assert translate_term("2019年05月07日至 年 月 日") == "May 7, 2019 to Long term"
    assert "(blank)" not in translate_term("2019年05月07日至")
