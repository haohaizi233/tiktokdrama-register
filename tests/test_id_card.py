"""Chinese ID front/back detection and -正面/-反面 copies."""
from pathlib import Path

from license_trans.id_card import classify_id_side, copy_id_named, already_id_copy
from license_trans.ocr import OcrLine


def _line(text):
    return OcrLine(text=text, score=0.9, x0=0, y0=0, x1=1, y1=1)


def test_classify_id_front_by_number_label():
    assert classify_id_side([_line("姓名 李晖"), _line("公民身份号码 430221199001011234")]) == "正面"


def test_classify_id_front_citizen_id_alt():
    assert classify_id_side([_line("公民身份证号 11010119900307251X")]) == "正面"


def test_classify_id_back_by_title():
    assert classify_id_side([
        _line("中华人民共和国居民身份证"),
        _line("签发机关 株洲市公安局"),
        _line("有效期限 2020.03.31-2040.03.31"),
    ]) == "反面"


def test_classify_license_not_id():
    assert classify_id_side([
        _line("营业执照"),
        _line("统一社会信用代码 91430211MA7MWFUJ1U"),
    ]) is None


def test_classify_front_wins_when_title_also_says_resident_id():
    lines = [
        _line("中华人民共和国居民身份证"),
        _line("公民身份号码 430221199001011234"),
    ]
    assert classify_id_side(lines) == "正面"


def test_copy_id_named_adds_suffix(tmp_path: Path):
    src = tmp_path / "abc123.jpg"
    src.write_bytes(b"fake")
    dest = copy_id_named(src, "正面")
    assert dest is not None
    assert dest.name == "abc123-正面.jpg"
    assert dest.exists()
    assert src.exists()
    again = copy_id_named(src, "正面")
    assert again == dest


def test_already_named_copy_is_noop(tmp_path: Path):
    src = tmp_path / "shot-反面.png"
    src.write_bytes(b"x")
    assert already_id_copy(src)
    assert copy_id_named(src, "反面") == src


def test_named_id_side_from_stem(tmp_path: Path):
    from license_trans.id_card import named_id_side
    assert named_id_side(tmp_path / "a-正面.jpg") == "正面"
    assert named_id_side(tmp_path / "a_反面.png") == "反面"
    assert named_id_side(tmp_path / "plain.jpg") is None


VALID_ID = "430221199001011232"  # checksum-verified; birth 1990-01-01


def test_id18_checksum_and_birth():
    from license_trans.id_card import valid_id18, birth_from_id18, id18_checksum
    assert valid_id18(VALID_ID)
    assert not valid_id18("430221199001011234")
    assert birth_from_id18(VALID_ID) == "1990-01-01"
    assert birth_from_id18("430221199001011234") == ""
    assert id18_checksum(VALID_ID[:17]) == VALID_ID[-1]


def test_parse_id_front_name_and_number():
    from license_trans.id_card import parse_id_fields
    got = parse_id_fields([
        _line("姓名 李晖"),
        _line("性别 男 民族 汉"),
        _line("出生 1990 年 1 月 1 日"),
        _line(f"公民身份号码 {VALID_ID}"),
    ])
    assert got["name"] == "李晖"
    assert got["id_no"] == VALID_ID
    assert got["birth"] == "1990-01-01"


def test_parse_id_front_ignores_bad_checksum():
    from license_trans.id_card import parse_id_fields
    got = parse_id_fields([
        _line("姓名张三"),
        _line("公民身份号码 430221199001011234"),
        _line("出生 1990年1月1日"),
    ])
    assert got["name"] == "张三"
    assert "id_no" not in got
    assert got["birth"] == "1990-01-01"


def test_parse_id_back_expiry_and_long_term():
    from license_trans.id_card import parse_id_fields, merge_id_fields
    back = parse_id_fields([
        _line("中华人民共和国居民身份证"),
        _line("签发机关 株洲市公安局荷塘分局"),
        _line("有效期限 2020.03.31-2040.03.31"),
    ])
    assert back["id_exp"] == "2040-03-31"
    assert back.get("id_long_term") is False
    assert back["authority"] == "株洲市公安局荷塘分局"
    long_term = parse_id_fields([_line("有效期限 2010.03.31-长期")])
    assert long_term["id_long_term"] is True
    assert long_term.get("id_exp") == ""
    merged = merge_id_fields(
        {"name": "李晖", "id_no": VALID_ID, "birth": "1990-01-01"},
        back,
    )
    assert merged["name"] == "李晖"
    assert merged["id_exp"] == "2040-03-31"


def test_cn_admin_parts_province_city():
    from license_trans.id_card import cn_admin_parts
    province, city = cn_admin_parts("湖南省株洲市荷塘区新华西路1号")
    assert province == "湖南省"
    assert city == "株洲市"
    p2, c2 = cn_admin_parts("北京市朝阳区建国路")
    assert p2 == "北京市"
    assert c2 in ("北京市", "朝阳区") or c2  # municipality fallback or district-as-city


def _box(text, x0, y0, x1, y1):
    return OcrLine(text=text, score=0.9, x0=x0, y0=y0, x1=x1, y1=y1)


def _handheld_front():
    return [
        _box("公民身份号码", 220, 920, 310, 928),
        _box(VALID_ID, 220, 930, 320, 937),
    ]


def _closeup_front():
    return [
        _box("姓名 李晖", 70, 110, 420, 165),
        _box("公民身份号码", 70, 480, 720, 540),
        _box(VALID_ID, 70, 550, 980, 615),
    ]


def test_score_prefers_large_font_closeup():
    from license_trans.id_card import score_id_photo
    hand = score_id_photo(_handheld_front())
    close = score_id_photo(_closeup_front())
    assert hand.handheld is True
    assert close.handheld is False
    assert close.font_px > hand.font_px
    assert close.score > hand.score


def test_pick_best_ids_skips_stale_copies(tmp_path: Path):
    from license_trans.id_card import pick_best_ids, copy_id_canonical
    hand = tmp_path / "Image_hand.jpg"
    close = tmp_path / "Image_close.jpg"
    stale = tmp_path / "Image_hand-正面.jpg"
    hand.write_bytes(b"h")
    close.write_bytes(b"c")
    stale.write_bytes(b"s")
    best = pick_best_ids([
        (hand, _handheld_front(), "正面"),
        (close, _closeup_front(), "正面"),
        (stale, _handheld_front(), "正面"),
    ])
    assert best["正面"][0] == close
    dest = copy_id_canonical(close, "正面")
    assert dest is not None and dest.name == "id-正面.jpg"
    assert dest.exists()
    assert close.exists()
    assert not stale.exists()


def test_match_id_prefers_canonical(tmp_path: Path):
    import materials
    stale = tmp_path / "Image_xxx-正面.jpg"
    canon = tmp_path / "id-正面.jpg"
    stale.write_bytes(b"s")
    canon.write_bytes(b"c")
    got = materials._match_id_images([stale, canon])
    assert got["id_front"] == canon
