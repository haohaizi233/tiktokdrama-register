"""AI does not invent Chinese facts; retry keeps existing values; OCR/AI fill leftover dates."""
from unittest.mock import MagicMock, PropertyMock

from license_trans.ai_validator import LicenseSemanticValidator, TranslationValidator, VALID_ACTIONS
from license_trans.field_state import FieldStateTracker, FieldStatus
from license_trans.parse import LicenseFields
from license_trans.pipeline import _apply_qr_usci, _run_ai_validation_and_retry
from license_trans.canonical import CanonicalChineseRecord
from license_trans.translate import translate_fields
from license_trans.ocr import OcrLine
import numpy as np


def test_ai_rule_repair_does_not_write_chinese():
    client = MagicMock()
    type(client).is_available = PropertyMock(return_value=True)
    client.chat_json = MagicMock(return_value={
        "overall_status": "retry_required",
        "issues": [{
            "field": "scope",
            "type": "suspected_ocr_error",
            "fragment": "依法直主",
            "expected_pattern": "某个AI猜出来的值",
            "confidence": 0.95,
            "action": "rule_repair",
            "reason": "AI guess",
        }],
    })
    validator = LicenseSemanticValidator(client)
    tracker = FieldStateTracker()
    original = "依法直主开展经营活动"
    tracker.init_field("scope", original)
    result = validator.validate({}, [], [])
    retry = validator.apply_to_tracker(result, tracker)
    fs = tracker.get("scope")
    assert fs.value == original
    assert "某个AI猜出来的值" not in fs.value
    assert retry == ["scope"]
    assert fs.status == FieldStatus.RETRY_OCR
    assert result["issues"][0]["action"] == "retry_ocr"
    assert "expected_pattern" not in result["issues"][0]


def test_translation_validator_does_not_overwrite_english():
    validator = TranslationValidator(MagicMock())
    result = {
        "overall_status": "needs_review",
        "issues": [{
            "field": "address",
            "action": "rule_repair",
            "suggested": "Erdao District, Changchun City",
        }],
    }
    issues = result["issues"]
    # sanitize via validate path
    client = MagicMock()
    type(client).is_available = PropertyMock(return_value=True)
    client.chat_json = MagicMock(return_value=result)
    validator = TranslationValidator(client)
    out = validator.validate({"address": "长春市二道区"}, {"address": "Changchun City Erdao District"})
    english = {"address": "Changchun City Erdao District"}
    flags = validator.collect_flags(out)
    assert english["address"] == "Changchun City Erdao District"
    assert flags.get("address") == "web_verify"
    assert not hasattr(validator, "apply_corrections")


def test_retry_keeps_existing_value_without_review(monkeypatch):
    fields = LicenseFields(scope="依法直主开展经营活动", name="测试有限公司")
    client_results = [{
        "overall_status": "retry_required",
        "issues": [{"field": "scope", "action": "retry_ocr", "fragment": "依法直主"}],
    }]

    class FakeClient:
        def is_available(self):
            return True

        def chat_json(self, *a, **k):
            return client_results[0]

    import license_trans.pipeline as pipeline

    monkeypatch.setattr(pipeline, "AIClient", lambda config: FakeClient())
    monkeypatch.setattr(pipeline, "_field_level_retry", lambda *a, **k: [])

    tracker = _run_ai_validation_and_retry(
        fields,
        [OcrLine("经营范围 依法直主开展经营活动", 0.9, 0, 0, 10, 10)],
        np.zeros((20, 20, 3), dtype=np.uint8),
        {},
        engine=None,
    )
    fs = tracker.get("scope")
    assert fs.status == FieldStatus.OK
    assert "开展经营活动" in fields.scope
    assert "scope" not in tracker.fields_needing_review()


def test_second_validation_after_successful_retry(monkeypatch):
    fields = LicenseFields(scope="依法直主开展经营活动", name="测试有限公司")
    calls = {"n": 0}

    class FakeClient:
        def is_available(self):
            return True

        def chat_json(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "overall_status": "retry_required",
                    "issues": [{"field": "scope", "action": "retry_ocr"}],
                }
            return {"overall_status": "ok", "issues": []}

    import license_trans.pipeline as pipeline

    def fake_retry(retry_fields, fields, lines, oriented, engine, tracker, trace=None):
        fields.scope = "依法自主开展经营活动"
        fs = tracker.get("scope")
        fs.add_repair("依法直主开展经营活动", fields.scope, "ocr_fusion")
        fs.status = FieldStatus.OK
        return ["scope"]

    monkeypatch.setattr(pipeline, "AIClient", lambda config: FakeClient())
    monkeypatch.setattr(pipeline, "_field_level_retry", fake_retry)

    tracker = _run_ai_validation_and_retry(
        fields,
        [OcrLine("scope", 0.9, 0, 0, 10, 10)],
        np.zeros((20, 20, 3), dtype=np.uint8),
        {},
        engine=None,
    )
    assert calls["n"] >= 2
    assert fields.scope == "依法自主开展经营活动"
    assert tracker.get("scope").status in (FieldStatus.OK, FieldStatus.RULE_REPAIRED)


def test_qr_fills_credit_code_before_canonical_and_english():
    usci = "91110101MA004KQM97"
    fields = LicenseFields(name="测试有限公司", credit_code="")
    _apply_qr_usci(fields, usci, f"https://www.gsxt.gov.cn/{usci}")
    canonical = CanonicalChineseRecord.from_fields(fields)
    english = translate_fields(canonical.to_translation_input())
    assert fields.credit_code == usci
    assert canonical.credit_code == usci
    assert english["credit_code"] == usci


def test_translation_reads_canonical_not_raw_fields():
    raw = LicenseFields(name="原始名称有限公司", credit_code="91110101MA004KQM97")
    canonical = CanonicalChineseRecord.from_fields(raw)
    canonical.name = "规范名称有限公司"
    canonical.freeze()
    english = translate_fields(canonical.to_translation_input())
    assert english["source"]["name"] == "规范名称有限公司"
    assert "原始名称" not in (english.get("name") or "")


def test_canonical_frozen_cannot_mutate():
    rec = CanonicalChineseRecord.from_fields(
        LicenseFields(name="测试有限公司", credit_code="91110101MA004KQM97")
    )
    rec.freeze()
    raised = False
    try:
        rec.name = "被改掉"
    except AttributeError:
        raised = True
    assert raised
    assert rec.name == "测试有限公司"


def test_scope_broken_ignores_truncated_legal_footer():
    from license_trans.pipeline import _scope_broken
    truncated = (
        "般项目:建筑材料销售:金属材料销售"
        "(除依法须经批准的项目外，凭营业执照依法自主开展经营活"
    )
    assert _scope_broken(truncated) is False
    nested = "食用农产品（不（依法须经批准的项目外）"
    assert _scope_broken(nested) is True


def test_scope_watermark_fails_rules():
    from license_trans.rule_repair import field_passes_rules
    junk = "经营范般项日:CJDGL"
    good = "般项目:建筑材料销售:金属材料销售:木材销售"
    assert field_passes_rules("scope", junk) is False
    assert field_passes_rules("scope", good) is True


def test_ai_retry_list_is_deduped(monkeypatch):
    fields = LicenseFields(
        scope="经营范般项日:CJDGL",
        name="测试有限公司",
        address="山东省青岛市黄岛区团结路1号",
    )
    retries = []

    class FakeClient:
        def is_available(self):
            return True

        def chat_json(self, *a, **k):
            return {
                "overall_status": "retry_required",
                "issues": [
                    {"field": "scope", "action": "retry_ocr"},
                    {"field": "scope", "action": "retry_ocr"},
                    {"field": "scope", "action": "retry_ocr"},
                    {"field": "address", "action": "retry_ocr"},
                ],
            }

    import license_trans.pipeline as pipeline

    def fake_retry(retry_fields, *a, **k):
        retries.append(list(retry_fields))
        return ["scope"]

    monkeypatch.setattr(pipeline, "AIClient", lambda config: FakeClient())
    monkeypatch.setattr(pipeline, "_field_level_retry", fake_retry)
    _run_ai_validation_and_retry(
        fields,
        [OcrLine("经营范围 般项目", 0.9, 0, 0, 10, 10)],
        np.zeros((20, 20, 3), dtype=np.uint8),
        {},
        engine=None,
    )
    assert retries
    assert retries[0].count("scope") == 1


def test_display_view_keeps_ocr_orientation():
    """Already-upright 横版 stays landscape (title on top)."""
    from license_trans.pipeline import _display_view

    landscape = np.zeros((400, 700, 3), dtype=np.uint8)
    landscape[:, :] = (40, 80, 200)
    lines = [
        OcrLine("营业执照", 0.9, 40, 30, 180, 70),
        OcrLine("统一社会信用代码", 0.9, 40, 90, 280, 120),
        OcrLine("名称", 0.9, 40, 140, 100, 170),
        OcrLine("类型", 0.9, 40, 190, 100, 220),
        OcrLine("法定代表人", 0.9, 40, 240, 180, 270),
        OcrLine("登记机关", 0.9, 40, 300, 160, 330),
        OcrLine("2024年09月19日", 0.9, 400, 300, 620, 330),
    ]
    meta = {"vertical_native": True, "rotation": 1}
    view = _display_view(landscape, lines, meta)
    assert view.shape[1] >= view.shape[0]


def test_display_view_rotates_sideways_hengban():
    """Phone-portrait shot of a 横版 license becomes landscape, title on top."""
    from license_trans.pipeline import _display_view

    portrait = np.zeros((700, 400, 3), dtype=np.uint8)
    portrait[:, :] = (30, 30, 30)
    portrait[:, 330:400] = (0, 0, 255)  # red stripe = 营业执照 on the right
    lines = [
        OcrLine("营业执照", 0.9, 330, 80, 395, 520),
        OcrLine("统一社会信用代码", 0.9, 200, 40, 310, 200),
        OcrLine("名称", 0.9, 180, 80, 250, 160),
        OcrLine("类型", 0.9, 180, 180, 250, 250),
        OcrLine("法定代表人", 0.9, 180, 260, 250, 360),
        OcrLine("登记机关", 0.9, 40, 200, 100, 320),
        OcrLine("2024年09月19日", 0.9, 20, 400, 80, 620),
    ]
    view = _display_view(portrait, lines, {"rotation": 0, "vertical_native": True})
    assert view.shape[1] > view.shape[0]
    # After rot90 CCW the right-edge stripe becomes the top edge.
    top = view[0:max(8, view.shape[0] // 12), :]
    assert int(top[:, :, 2].mean()) > int(top[:, :, 0].mean())


def test_orientation_score_prefers_title_on_top():
    from license_trans.ocr import _orientation_score

    sideways = [
        OcrLine("营业执照", 0.9, 320, 80, 390, 520),
        OcrLine("名称", 0.9, 180, 80, 220, 200),
        OcrLine("类型", 0.9, 180, 220, 220, 320),
        OcrLine("法定代表人", 0.9, 180, 340, 220, 480),
    ]
    upright = [
        OcrLine("营业执照", 0.9, 40, 20, 280, 80),
        OcrLine("名称", 0.9, 40, 100, 180, 130),
        OcrLine("类型", 0.9, 40, 150, 180, 180),
        OcrLine("法定代表人", 0.9, 40, 200, 220, 230),
        OcrLine("经营范围", 0.9, 40, 250, 180, 280),
    ]
    assert _orientation_score(upright, 400) > _orientation_score(sideways, 700)


def test_recover_issue_date_does_not_steal_established():
    from license_trans.pipeline import _recover_issue_date
    h, w = 800, 1000
    lines = [
        OcrLine("成立日期", 0.9, 50, 100, 150, 120),
        OcrLine("2018年09月12日", 0.9, 160, 100, 320, 120),
        OcrLine("登记机关", 0.9, 80, 550, 200, 580),
        OcrLine("2024", 0.9, 700, 590, 740, 610),
        OcrLine("23", 0.9, 800, 590, 830, 610),
    ]
    img = np.zeros((h, w, 3), dtype=np.uint8)

    class _Eng:
        def _ocr(self, _im):
            return [], 0.0

    issue = _recover_issue_date(img, lines, _Eng())
    assert issue != "2018年09月12日"
    assert issue == ""


def test_stitch_issue_date_tokens_ltr():
    from license_trans.pipeline import _stitch_issue_date_tokens
    lines = [
        OcrLine("2024", 0.9, 10, 4, 50, 22),
        OcrLine("年", 0.9, 54, 4, 72, 22),
        OcrLine("09", 0.9, 80, 4, 110, 22),
        OcrLine("月", 0.9, 114, 4, 132, 22),
        OcrLine("19", 0.9, 140, 4, 170, 22),
        OcrLine("日", 0.9, 174, 4, 192, 22),
    ]
    assert _stitch_issue_date_tokens(lines) == "2024年09月19日"
    assert _stitch_issue_date_tokens([
        OcrLine("2024年09月19日", 0.9, 10, 4, 200, 22),
    ]) == "2024年09月19日"
    assert _stitch_issue_date_tokens([
        OcrLine("2024年1月1日", 0.9, 10, 4, 200, 22),
    ]) == "2024年01月01日"


def test_issue_date_strip_is_under_authority_seal():
    from license_trans.pipeline import _issue_date_strip
    img = np.zeros((800, 1200, 3), dtype=np.uint8)
    img[:, :] = (240, 240, 240)
    lines = [
        OcrLine("登记机关", 0.9, 40, 620, 160, 650),
        OcrLine("青岛市市场监督管理局", 0.9, 40, 660, 360, 690),
    ]
    crop = _issue_date_strip(img, lines)
    assert crop is not None
    assert crop.shape[1] < 1200
    assert crop.shape[0] < 800
    # Right-hand seal band, not the 成立日期 row at the top.
    assert crop.shape[1] <= 1200 * 0.55 + 2


def test_recover_issue_date_destains_and_stitches(monkeypatch):
    from license_trans import pipeline as pipe

    destain_calls = []
    real_destain = pipe._remove_red_seal

    def spy_destain(img):
        destain_calls.append(img.shape)
        return real_destain(img)

    monkeypatch.setattr(pipe, "_remove_red_seal", spy_destain)

    img = np.zeros((800, 1000, 3), dtype=np.uint8)
    img[:, :] = (230, 230, 230)
    img[560:740, 520:980] = (40, 40, 200)
    lines = [
        OcrLine("成立日期", 0.9, 40, 80, 140, 110),
        OcrLine("2018年09月12日", 0.9, 160, 80, 360, 110),
        OcrLine("登记机关", 0.9, 40, 560, 160, 590),
    ]

    class _Eng:
        def _ocr(self, _im):
            toks = [
                ([[8, 4], [40, 4], [40, 20], [8, 20]], "2024", 0.9),
                ([[44, 4], [60, 4], [60, 20], [44, 20]], "年", 0.9),
                ([[64, 4], [90, 4], [90, 20], [64, 20]], "09", 0.9),
                ([[94, 4], [110, 4], [110, 20], [94, 20]], "月", 0.9),
                ([[114, 4], [140, 4], [140, 20], [114, 20]], "19", 0.9),
                ([[144, 4], [160, 4], [160, 20], [144, 20]], "日", 0.9),
            ]
            return toks, 0.0

    issue = pipe._recover_issue_date(img, lines, _Eng())
    assert destain_calls
    assert issue == "2024年09月19日"


def test_harvest_dates_and_term_from_ocr():
    from license_trans.pipeline import _harvest_from_ocr
    from license_trans.field_state import FieldStateTracker

    fields = LicenseFields(name="横峰县育民建筑劳务服务有限公司")
    tracker = FieldStateTracker()
    for name in ("established", "term", "issue_date", "authority", "scope"):
        tracker.init_field(name, "")
    _harvest_from_ocr(fields, tracker, [
        "名称横峰县育民建筑劳务服务有限公司",
        "成立日期2021年03月24日",
        "营业期限2021年03月24日至长期",
        "登记机关横峰县市场监督管理局",
        "发证日期2021年03月24日",
    ])
    assert fields.established == "2021年03月24日"
    assert fields.term == "2021年03月24日至长期"
    assert fields.issue_date == "2021年03月24日"
    assert fields.authority == "横峰县市场监督管理局"
    assert tracker.get("established").status == FieldStatus.OK


def test_harvest_completes_date_only_term():
    from license_trans.pipeline import _harvest_from_ocr
    from license_trans.field_state import FieldStateTracker
    from license_trans.rule_repair import field_passes_rules

    assert field_passes_rules("term", "2021年03月24日") is False
    assert field_passes_rules("term", "2021年03月24日至长期") is True
    assert field_passes_rules("scope", "横峰县育民建筑劳务服务有限公司") is False

    fields = LicenseFields(
        name="横峰县育民建筑劳务服务有限公司",
        term="2021年03月24日",
        scope="横峰县育民建筑劳务服务有限公司",
    )
    tracker = FieldStateTracker()
    tracker.init_field("term", fields.term)
    tracker.init_field("scope", fields.scope)
    _harvest_from_ocr(fields, tracker, [
        "名称横峰县育民建筑劳务服务有限公司",
        "经营范围许可项目：建筑劳务分包（依法须经批准的项目，经相关部门批准后方可开展经营活动）一般项目：劳务服务（不含劳务派遣），机械设备租赁（除许可业务外，可自主依法经营法律法规非禁止或限制的项目）",
        "营业期限2021年03月24日至长期",
        "登记机关横峰县市场监督管理局",
    ])
    assert fields.term == "2021年03月24日至长期"
    assert "许可项目" in fields.scope
    assert "建筑劳务分包" in fields.scope
    assert "有限公司" not in fields.scope[-6:]


def test_fallback_issue_date_uses_established():
    from license_trans.pipeline import _fallback_issue_from_established
    from license_trans.field_state import FieldStateTracker

    fields = LicenseFields(established="2021年03月24日", issue_date="")
    tracker = FieldStateTracker()
    tracker.init_field("issue_date", "")
    _fallback_issue_from_established(fields, tracker)
    assert fields.issue_date == "2021年03月24日"
    assert tracker.get("issue_date").status == FieldStatus.OK


def test_harvest_scope_from_left_column_lines():
    from license_trans.pipeline import _harvest_from_ocr
    from license_trans.field_state import FieldStateTracker

    fields = LicenseFields(
        name="横峰县育民建筑劳务服务有限公司",
        scope="横峰县育民建筑劳务服务有限公司",
    )
    tracker = FieldStateTracker()
    tracker.init_field("scope", fields.scope)
    lines = [
        OcrLine("名称", 0.9, 20, 40, 80, 70),
        OcrLine("横峰县育民建筑劳务服务有限公司", 0.9, 90, 40, 400, 70),
        OcrLine("注册资本", 0.9, 520, 40, 600, 70),
        OcrLine("壹佰万元整", 0.9, 610, 40, 720, 70),
        OcrLine("经营范围", 0.9, 20, 200, 100, 230),
        OcrLine("许可项目：建筑劳务分包（依法须经批准的项目，经相关部门批准后方可开展经营活动）", 0.9, 110, 200, 480, 280),
        OcrLine("一般项目：劳务服务（不含劳务派遣），机械设备租赁", 0.9, 110, 280, 480, 330),
        OcrLine("住所", 0.9, 520, 200, 580, 230),
        OcrLine("江西省上饶市横峰县", 0.9, 590, 200, 780, 230),
    ]
    _harvest_from_ocr(fields, tracker, [ln.text for ln in lines], lines)
    assert "许可项目" in fields.scope
    assert "建筑劳务分包" in fields.scope
    assert "横峰县育民" not in fields.scope


def test_harvest_does_not_replace_good_food_scope_with_address_leak():
    from license_trans.pipeline import _harvest_from_ocr
    from license_trans.field_state import FieldStateTracker

    good = (
        "销售:预包装食品、散装食品(食品经营)、食用农产品(不含活禽):生产:瓶(罐)装饮用水(食品生产)"
        "(依法须经批准的项目，经相关部门批准后方可开展经营活动。)"
    )
    fields = LicenseFields(
        name="开平市森泉贸易有限公司",
        scope=good,
        type="型",
        address="开平市大沙镇新星村委会新星小学办公楼旧址1号",
        credit_code="91440783MA53XQRYXQ",
    )
    tracker = FieldStateTracker()
    tracker.init_field("scope", fields.scope)
    tracker.init_field("type", fields.type)
    lines = [
        OcrLine("类型", 0.9, 193, 638, 354, 673),
        OcrLine("型", 0.9, 319, 638, 354, 673),
        OcrLine("有限责任公司(自然人独资)", 0.99, 373, 638, 634, 667),
        OcrLine("经营范围", 0.9, 193, 730, 354, 760),
        OcrLine("销售:预包装食品、散装食品(食品经营)、食用农产品(不", 0.9, 373, 730, 834, 760),
        OcrLine("含活禽):生产:瓶(罐)装饮用水(食品生产)(依法须", 0.9, 373, 765, 834, 793),
        OcrLine("住", 0.9, 852, 752, 887, 787),
        OcrLine("所 开平市大沙镇新星村委会新星小学", 0.9, 900, 730, 1200, 760),
        OcrLine("办公楼旧址1号", 0.99, 1032, 772, 1180, 800),
        OcrLine("经批准的项目，经相关部门批准后方可开展经营活动。)", 0.9, 373, 785, 814, 813),
    ]
    _harvest_from_ocr(fields, tracker, [ln.text for ln in lines], lines)
    assert "预包装食品" in fields.scope
    assert "办公楼旧址" not in fields.scope
    assert "许可项目住" not in fields.scope
    assert "有限责任公司" in fields.type
    assert fields.type != "型"


def test_snap_kaiping_city_not_district():
    from license_trans.divisions import snap_text, places_from_usci

    addr = "开平市大沙镇新星村委会新星小学办公楼旧址1号"
    prefer = places_from_usci("91440783MA53XQRYXQ")
    snapped = snap_text(addr, prefer)
    assert "开平市" in snapped
    assert "开平区" not in snapped
    assert snap_text(addr) == snapped or "开平市" in snap_text(addr)


def test_translate_term_keeps_long_term():
    from license_trans.translate import translate_term
    assert translate_term("2021年03月24日至长期") == "March 24, 2021 to Long term"
    assert translate_term("2021年03月24日") == "March 24, 2021"
    assert translate_term("2019年05月07日至") == "May 7, 2019 to Long term"
    assert "(blank)" not in translate_term("2019年05月07日至 年 月 日")


def test_worth_field_retry_skips_valid_usci_and_authority():
    from license_trans.pipeline import _worth_field_retry

    fields = LicenseFields(
        credit_code="91361125MA3ABHY985",
        authority="横峰县市场监督管理局",
        name="横峰县育民建筑劳务服务有限公司",
        scope="横峰县育民建筑劳务服务有限公司",
    )
    assert _worth_field_retry("credit_code", fields) is False
    assert _worth_field_retry("authority", fields) is False
    assert _worth_field_retry("scope", fields) is False


def test_g_prefix_usci_fails_rules_so_harvest_can_replace():
    from license_trans.rule_repair import field_passes_rules
    from license_trans.usci import checksum_ok

    fake = "GL9143D211MA7MWFUJ"
    assert checksum_ok(fake)
    assert field_passes_rules("credit_code", fake) is False
    assert field_passes_rules("credit_code", "91430211MA7MWFUJ1U") is True


def test_harvest_replaces_g_prefix_usci_from_ocr_line():
    from license_trans.pipeline import _harvest_from_ocr
    from license_trans.field_state import FieldStateTracker

    real = "91430211MA7MWFUJ1U"
    fields = LicenseFields(name="株洲市浩航商贸有限公司", credit_code="GL9143D211MA7MWFUJ")
    tracker = FieldStateTracker()
    tracker.init_field("credit_code", fields.credit_code)
    lines = [
        OcrLine("9HO21MAMUUDGL", 0.4, 50, 134, 300, 180),
        OcrLine(real, 0.95, 51, 140, 218, 158),
    ]
    _harvest_from_ocr(fields, tracker, [ln.text for ln in lines], lines)
    assert fields.credit_code == real


def test_jindikang_address_not_harvested_as_scope():
    from license_trans.pipeline import _harvest_from_ocr, _harvest_scope_text
    from license_trans.field_state import FieldStateTracker
    from license_trans.rule_repair import field_passes_rules, scope_text_is_plausible

    url = "hip:/ww.gas.gov."
    leaked = "许可项目所陕西省宝鸡市高新开发区高新十创意大厦A座1单元0601号乐发区行审批专用章"
    assert field_passes_rules("scope", url) is False
    assert field_passes_rules("scope", leaked) is False
    assert _harvest_scope_text(
        "经营范围所陕西省宝鸡市高新开发区高新十创意大厦A座1单元0601号乐发区行审批专用章登记机关"
    ) == ""

    fields = LicenseFields(
        name="宝鸡金迪康机械加工有限公司",
        scope=url,
        address="陕西省宝鸡市高新开发区高新十创意大厦A座1单元0601号",
    )
    tracker = FieldStateTracker()
    tracker.init_field("scope", fields.scope)
    lines = [
        OcrLine("经营范围", 0.95, 313, 440, 411, 461),
        OcrLine("经营范国人", 0.47, 312, 425, 755, 486),
        OcrLine("所 陕西省宝鸡市高新开发区高新十", 0.97, 866, 426, 1071, 448),
        OcrLine("创意大厦A座1单元0601号", 0.99, 893, 440, 1035, 461),
        OcrLine("审批专用章", 0.85, 969, 520, 1052, 566),
        OcrLine("登记机关", 0.98, 860, 533, 971, 555),
        OcrLine("hip:/ww.gas.gov.", 0.6, 420, 666, 535, 695),
    ]
    _harvest_from_ocr(fields, tracker, [ln.text for ln in lines], lines)
    assert "创意大厦" not in (fields.scope or "")
    assert "审批专用章" not in (fields.scope or "")
    assert scope_text_is_plausible(fields.scope or "") is False


def test_fangzhang_address_not_harvested_as_scope():
    from license_trans.pipeline import _harvest_from_ocr, _harvest_scope_text
    from license_trans.field_state import FieldStateTracker
    from license_trans.rule_repair import field_passes_rules, scope_text_is_plausible

    leaked = (
        "许可项目所山东省潍坊市诸城市密州街道和平北街清明上河园15-1-007号"
        "方可开展经营活动)"
    )
    assert field_passes_rules("scope", leaked) is False
    assert _harvest_scope_text(
        "经营范围所山东省潍坊市诸城市密州街道和平北街清明上河园15-1-007号登记机关"
    ) == ""

    good = "销售首饰、工艺品、家具、茶叶、日用陶瓷器皿、皮革制品"
    fields = LicenseFields(
        name="山东放杖溪山商贸有限公司",
        scope=leaked,
        address="山东省潍坊市诸城市密州街道和平北街清明上河园15-1-007号",
        term="2019年05月07日至",
    )
    tracker = FieldStateTracker()
    tracker.init_field("scope", fields.scope)
    tracker.init_field("term", fields.term)
    lines = [
        OcrLine("经营范围", 0.9, 40, 200, 120, 230),
        OcrLine("销售首饰、工艺品、家具、茶叶、日用陶瓷器皿、皮革制品", 0.9, 130, 200, 480, 260),
        OcrLine("所 山东省潍坊市诸城市密州街道和平北街", 0.9, 520, 200, 780, 230),
        OcrLine("清明上河园15-1-007号", 0.9, 520, 230, 780, 260),
    ]
    _harvest_from_ocr(fields, tracker, [ln.text for ln in lines], lines)
    assert "15-1-007" not in (fields.scope or "")
    assert "许可项目所" not in (fields.scope or "")
    assert "销售首饰" in (fields.scope or "")
    assert scope_text_is_plausible(fields.scope) is True

def test_ai_fills_missing_dates_from_ocr(monkeypatch):
    fields = LicenseFields(name="测试有限公司", established="", term="", issue_date="")

    class FakeClient:
        def is_available(self):
            return True

        def chat_json(self, prompt, *_a, **_k):
            if "Copy existing OCR" in prompt or "copy these fields" in prompt:
                return {"fields": {
                    "established": "2021年03月24日",
                    "term": "2021年03月24日至长期",
                    "issue_date": "2021年03月24日",
                    "authority": "横峰县市场监督管理局",
                    "scope": "劳务分包、设备租赁",
                }}
            return {"overall_status": "ok", "issues": []}

    import license_trans.pipeline as pipeline
    monkeypatch.setattr(pipeline, "AIClient", lambda config: FakeClient())
    monkeypatch.setattr(pipeline, "_field_level_retry", lambda *a, **k: [])
    tracker = _run_ai_validation_and_retry(
        fields,
        [
            OcrLine("名称 测试有限公司", 0.9, 0, 0, 10, 10),
            OcrLine("经营范围 劳务分包、设备租赁", 0.9, 0, 20, 10, 30),
            OcrLine("成立日期 2021年03月24日", 0.9, 0, 40, 10, 50),
            OcrLine("营业期限 2021年03月24日至长期", 0.9, 0, 60, 10, 70),
            OcrLine("发证日期 2021年03月24日", 0.9, 0, 80, 10, 90),
            OcrLine("横峰县市场监督管理局", 0.9, 0, 100, 10, 110),
        ],
        np.zeros((20, 20, 3), dtype=np.uint8),
        {},
        engine=None,
    )
    assert fields.established == "2021年03月24日"
    assert "至长期" in fields.term
    assert fields.issue_date == "2021年03月24日"
    assert "局" in fields.authority
    assert tracker.fields_needing_review() == []


def test_infer_scope_region_without_scope_label():
    from license_trans.field_crop import infer_scope_region, FieldCropper

    img = np.zeros((1100, 1600, 3), dtype=np.uint8)
    lines = [
        OcrLine("法定代表人尹益高", 0.9, 260, 659, 468, 688),
        OcrLine("注册资本壹佰万元整", 0.9, 899, 593, 1154, 621),
        OcrLine("所 江西省上饶市横峰县岑阳镇人民大道", 0.9, 1013, 713, 1374, 741),
        OcrLine("制的项目)", 0.9, 406, 746, 481, 768),
        OcrLine("D登记机关", 0.9, 866, 873, 1208, 908),
    ]
    region = infer_scope_region(img, lines)
    assert region is not None
    x0, y0, x1, y1 = region
    assert y0 <= 688
    assert y1 >= 768
    assert x0 < 406
    assert x1 > 481
    assert x1 < 1013

    cropper = FieldCropper(img, lines)
    assert cropper.find_field_region("scope") == region
    auth = cropper.find_field_region("authority")
    assert auth is not None
    assert auth[0] <= 866
    assert auth[2] >= 1200


def test_recover_scope_from_left_column_crop(monkeypatch):
    from license_trans import pipeline as pipe

    img = np.zeros((1100, 1600, 3), dtype=np.uint8)
    img[:, :] = (240, 240, 240)
    lines = [
        OcrLine("法定代表人尹益高", 0.9, 260, 659, 468, 688),
        OcrLine("注册资本壹佰万元整", 0.9, 899, 593, 1154, 621),
        OcrLine("所 江西省上饶市横峰县岑阳镇人民大道", 0.9, 1013, 713, 1374, 741),
        OcrLine("制的项目)", 0.9, 406, 746, 481, 768),
        OcrLine("D登记机关", 0.9, 866, 873, 1208, 908),
    ]
    body = (
        "许可项目：建筑劳务分包（依法须经批准的项目，经相关部门批准后方可开展经营活动）"
        "一般项目：劳务服务（不含劳务派遣），机械设备租赁（除许可业务外，可自主依法经营法律法规非禁止或限制的项目）"
    )

    class _Eng:
        def _ocr(self, _im):
            return [([[8, 4], [400, 4], [400, 20], [8, 20]], body, 0.9)], 0.0

    text, extra = pipe._recover_scope(img, lines, _Eng())
    assert "许可项目" in text
    assert "建筑劳务分包" in text
    assert "一般项目" in text
    assert extra


def test_repair_date_keeps_term_to_long():
    from license_trans.rule_repair import repair_field_value, apply_deterministic_repairs
    from license_trans.field_state import FieldStateTracker

    assert repair_field_value("term", "2021年03月24日至长期") is None
    assert repair_field_value("term", "2021年3月24日至长期") == (
        "2021年03月24日至长期", "date_format")
    fields = LicenseFields(term="2021年03月24日至长期")
    tracker = FieldStateTracker()
    tracker.init_field("term", fields.term)
    apply_deterministic_repairs(fields, tracker)
    assert fields.term == "2021年03月24日至长期"


def test_infer_authority_from_county_in_address():
    from license_trans.pipeline import _infer_authority_from_place, _harvest_from_ocr
    from license_trans.field_state import FieldStateTracker

    assert _infer_authority_from_place(
        "江西省上饶市横峰县岑阳镇人民大道259号",
        "横峰县育民建筑劳务服务有限公司",
    ) == "横峰县市场监督管理局"

    fields = LicenseFields(
        name="横峰县育民建筑劳务服务有限公司",
        address="江西省上饶市横峰县岑阳镇人民大道259号",
        authority="",
    )
    tracker = FieldStateTracker()
    tracker.init_field("authority", "")
    _harvest_from_ocr(fields, tracker, ["D登记机关", "名称横峰县育民建筑劳务服务有限公司"])
    assert fields.authority == "横峰县市场监督管理局"
