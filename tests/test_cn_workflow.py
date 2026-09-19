"""CN company profiles and review-sheet for failed AI arbitration."""
from pathlib import Path

import materials
from license_trans.pipeline import _arbitration_fail_reason
from license_trans.review_sheet import append_review_row, review_sheet_path


def test_match_id_images_zh_suffix(tmp_path: Path):
    front = tmp_path / "a-正面.jpg"
    back = tmp_path / "b-反面.jpg"
    front.write_bytes(b"f")
    back.write_bytes(b"b")
    got = materials._match_id_images([front, back])
    assert got["id_front"] == front
    assert got["id_back"] == back


def test_cn_folder_profile_uses_english_license_for_aog_and_cp(tmp_path, monkeypatch):
    monkeypatch.setattr(materials, "MATERIALS_DIR", tmp_path)
    folder = tmp_path / "株洲市浩航商贸有限公司李晖"
    folder.mkdir()
    pdf = folder / "shot英文版.pdf"
    pdf.write_bytes(b"%PDF")
    (folder / "id-正面.jpg").write_bytes(b"f")
    (folder / "id-反面.jpg").write_bytes(b"b")
    (folder / "cn_meta.json").write_text(
        '{"name_zh":"株洲市浩航商贸有限公司","name_en":"Zhuzhou Haohang Trading Co., Ltd.",'
        '"credit_code":"91430211MA7MWFUJ1U"}',
        encoding="utf-8",
    )
    profiles = materials.cn_folder_profiles()
    assert len(profiles) == 1
    p = profiles[0]
    assert p["workflow"] == "cn"
    assert p["complete"] is True
    assert p["biz_proof"].name == "shot英文版.pdf"
    assert p["cp575"] == p["biz_proof"]
    assert p["name_zh"] == "株洲市浩航商贸有限公司"
    assert p["reg_no"] == "91430211MA7MWFUJ1U"


def test_cn_folder_incomplete_without_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(materials, "MATERIALS_DIR", tmp_path)
    folder = tmp_path / "某公司"
    folder.mkdir()
    (folder / "x英文版.pdf").write_bytes(b"%PDF")
    p = materials.cn_folder_profiles()[0]
    assert p["complete"] is False
    assert any("id_front" in x for x in p["issues"])


def test_review_sheet_only_for_semantic_arbitration():
    from license_trans.pipeline import _is_ocr_transient, _is_semantic_review

    assert _is_semantic_review({}, "语义仲裁未通过: 经营范围像住所/印章/网址") is True
    assert _is_semantic_review({}, "缺失字段: credit_code,capital") is False
    assert _is_semantic_review({}, "翻译失败: WeChat OCR timed out after 30.0s") is False
    assert _is_semantic_review({}, "非A格式: 个体户") is False
    assert _is_ocr_transient(RuntimeError("WeChat OCR timed out after 30.0s")) is True
    assert _is_ocr_transient(RuntimeError("No OCR engine available")) is True
    assert _is_ocr_transient(RuntimeError("disk full")) is False


def test_arbitration_fail_on_missing_and_cjk():
    assert "缺失字段" in _arbitration_fail_reason({"fields": {"name": "A"}})
    ok = {
        "fields": {
            "name": "株洲市浩航商贸有限公司",
            "credit_code": "91430211MA7MWFUJ1U",
            "scope": "一般项目：销售",
            "legal_person": "李晖",
            "capital": "贰佰万元整",
            "warnings": [],
        },
        "english": {"name": "Zhuzhou Haohang Trading Co., Ltd.", "scope": "sales", "address": "Hunan", "legal_person": "Li Hui"},
        "needs_review": [],
    }
    assert _arbitration_fail_reason(ok) == ""
    bad = dict(ok)
    bad["english"] = dict(ok["english"], scope="一般项目 sales")
    assert "英译残留中文" in _arbitration_fail_reason(bad)


def test_cn_folder_profile_maps_id_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(materials, "MATERIALS_DIR", tmp_path)
    folder = tmp_path / "株洲市浩航商贸有限公司李晖"
    folder.mkdir()
    (folder / "shot英文版.pdf").write_bytes(b"%PDF")
    (folder / "id-正面.jpg").write_bytes(b"f")
    (folder / "id-反面.jpg").write_bytes(b"b")
    (folder / "cn_meta.json").write_text(
        '{"name_zh":"株洲市浩航商贸有限公司","name_en":"Zhuzhou Haohang Trading Co., Ltd.",'
        '"credit_code":"91430211MA7MWFUJ1U","id_name":"李晖",'
        '"id_no":"430221199001011232","id_birth":"1990-01-01",'
        '"id_exp":"2040-03-31","reg_state":"湖南省","reg_city":"株洲市"}',
        encoding="utf-8",
    )
    p = materials.cn_folder_profiles()[0]
    assert p["complete"] is True
    assert p["name_zh"] == "株洲市浩航商贸有限公司"
    assert p["reg_no"] == "91430211MA7MWFUJ1U"
    assert p["reg_state"] == "湖南省"
    assert p["reg_city"] == "株洲市"
    # 本地 OCR 身份证不写入填表槽, 认证时走站点识别
    assert p["rep_name"] == ""
    assert p["id_no"] == ""
    assert materials.has_id_preset(p) is False


def test_cn_xlsx_id_fields_are_preset(tmp_path, monkeypatch):
    monkeypatch.setattr(materials, "MATERIALS_DIR", tmp_path)
    folder = tmp_path / "株洲市浩航商贸有限公司"
    folder.mkdir()
    (folder / "shot英文版.pdf").write_bytes(b"%PDF")
    (folder / "id-正面.jpg").write_bytes(b"f")
    (folder / "id-反面.jpg").write_bytes(b"b")
    row = {
        "company": folder.name,
        "rep_name": "李晖",
        "rep_birth": "1990-01-01",
        "id_no": "430221199001011232",
        "id_exp": "2040-03-31",
    }
    p = materials._profile_from_folder(folder, row, workflow="cn")
    assert p["rep_name"] == "李晖"
    assert p["id_no"] == "430221199001011232"
    assert materials.has_id_preset(p) is True


def test_cn_expired_id_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(materials, "MATERIALS_DIR", tmp_path)
    folder = tmp_path / "某公司"
    folder.mkdir()
    (folder / "x英文版.pdf").write_bytes(b"%PDF")
    (folder / "id-正面.jpg").write_bytes(b"f")
    (folder / "id-反面.jpg").write_bytes(b"b")
    (folder / "cn_meta.json").write_text(
        '{"id_exp":"2020-01-01","id_long_term":false}', encoding="utf-8")
    p = materials.cn_folder_profiles()[0]
    assert p["complete"] is False
    assert any("过期" in x for x in p["issues"])


def test_cn_long_term_id_not_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(materials, "MATERIALS_DIR", tmp_path)
    folder = tmp_path / "某公司"
    folder.mkdir()
    (folder / "x英文版.pdf").write_bytes(b"%PDF")
    (folder / "id-正面.jpg").write_bytes(b"f")
    (folder / "id-反面.jpg").write_bytes(b"b")
    (folder / "cn_meta.json").write_text(
        '{"id_exp":"","id_long_term":true}', encoding="utf-8")
    p = materials.cn_folder_profiles()[0]
    assert p["complete"] is True


def test_write_cn_meta_includes_id_and_admin(tmp_path):
    from license_trans.pipeline import _write_cn_meta
    info = {
        "fields": {
            "name": "株洲市浩航商贸有限公司",
            "credit_code": "91430211MA7MWFUJ1U",
            "legal_person": "李晖",
            "address": "湖南省株洲市荷塘区新华西路1号",
        },
        "english": {"name": "Zhuzhou Haohang Trading Co., Ltd.", "address": "Hunan"},
        "output": str(tmp_path / "shot英文版.pdf"),
    }
    path = _write_cn_meta(tmp_path, info, {
        "name": "李晖", "id_no": "430221199001011232",
        "birth": "1990-01-01", "id_exp": "2040-03-31",
    })
    import json
    meta = json.loads(path.read_text(encoding="utf-8"))
    assert meta["id_name"] == "李晖"
    assert meta["id_no"] == "430221199001011232"
    assert meta["id_birth"] == "1990-01-01"
    assert meta["reg_state"] == "湖南省"
    assert meta["reg_city"] == "株洲市"


def test_append_review_row_creates_workbook(tmp_path: Path):
    path = append_review_row(
        tmp_path, folder="浩航", file="a.jpg", reason="缺失字段: scope",
        name="株洲市浩航商贸有限公司", credit_code="91430211MA7MWFUJ1U", missing="scope",
    )
    assert path == review_sheet_path(tmp_path)
    assert path.exists()
    import openpyxl
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    assert ws.cell(1, 1).value == "时间"
    assert ws.cell(2, 2).value == "浩航"
    assert "缺失字段" in (ws.cell(2, 4).value or "")
    wb.close()
    append_review_row(tmp_path, folder="第二家", reason="非A格式")
    wb = openpyxl.load_workbook(path)
    assert wb.active.max_row == 3
    wb.close()
