"""Field crop parse: complete-field candidates, label exclusion, scope merge."""
from license_trans.field_parse import is_label_only, parse_field_lines, strip_field_label
from license_trans.ocr import OcrLine
from license_trans.ocr_ensemble import FieldOcrCandidate, FusionResult, OcrCandidateFusion


def _ln(text, y0, x0=0, score=0.9):
    return OcrLine(text=text, score=score, x0=x0, y0=y0, x1=x0 + 200, y1=y0 + 20)


def test_scope_multiline_is_one_candidate_text():
    lines = [
        _ln("经营范围", 0),
        _ln("建筑材料销售；", 22),
        _ln("金属材料销售；", 44),
        _ln("机械设备租赁。", 66),
    ]
    text = parse_field_lines("scope", lines)
    assert text == "建筑材料销售；金属材料销售；机械设备租赁。"
    assert "经营范围" not in text


def test_labels_are_not_field_values():
    for field, label in (
        ("scope", "经营范围"),
        ("address", "住所"),
        ("name", "名称"),
        ("legal_person", "法定代表人"),
    ):
        assert is_label_only(label, field)
        assert strip_field_label(label, field) == ""
        assert parse_field_lines(field, [_ln(label, 0)]) == ""


def test_address_strips_label_and_joins():
    lines = [
        _ln("住所", 0),
        _ln("杭州市富阳区", 20),
        _ln("富春街道", 40),
    ]
    assert parse_field_lines("address", lines) == "杭州市富阳区富春街道"


def test_name_strips_label():
    lines = [_ln("名称 杭州测试科技有限公司", 0)]
    assert parse_field_lines("name", lines) == "杭州测试科技有限公司"


def test_fusion_does_not_count_variants_as_independent_engines():
    fusion = OcrCandidateFusion()
    candidates = [
        FieldOcrCandidate("scope", "wechat", "original", "A", None),
        FieldOcrCandidate("scope", "wechat", "clahe", "A", None),
        FieldOcrCandidate("scope", "wechat", "2x", "A", None),
        FieldOcrCandidate("scope", "ppocr_v6", "original", "B", 0.95),
    ]
    result = fusion.fuse_field_ex("scope", candidates)
    assert isinstance(result, FusionResult)
    assert result.engine_conflict is True
    assert result.independent_engines.get("wechat") == "A"
    assert result.independent_engines.get("ppocr_v6") == "B"
    # not 3 independent engines
    assert len(result.independent_engines) == 2
    wechat_variant_votes = result.variant_vote.get("A", 0)
    assert wechat_variant_votes == 3
    assert result.engine_vote.get("A", 0) == 1


def test_wechat_none_score_does_not_beat_ppocr_score():
    fusion = OcrCandidateFusion()
    candidates = [
        FieldOcrCandidate("name", "wechat", "original", "甲公司", None),
        FieldOcrCandidate("name", "ppocr_v6", "original", "乙公司", 0.95),
    ]
    result = fusion.fuse_field_ex("name", candidates)
    assert result.winner is not None
    # Independent engines conflict; PP-OCR's real 0.95 must not lose to fake 1.0
    # None score contributes 0, so 乙公司 wins on OCR score unless rules say otherwise.
    assert result.winner.text == "乙公司"
    assert result.winner.score == 0.95
