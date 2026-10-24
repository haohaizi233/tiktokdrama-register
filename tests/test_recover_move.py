# -*- coding: utf-8 -*-
"""已译未搬走补搬 + 执照库计数。"""
from pathlib import Path

from license_trans import pipeline


def _touch(p: Path, name: str) -> Path:
    f = p / name
    f.write_bytes(b"x")
    return f


def test_folder_ready_to_move(tmp_path):
    folder = tmp_path / "某某公司"
    folder.mkdir()
    assert not pipeline._folder_ready_to_move(folder)
    _touch(folder, "cn_meta.json")
    _touch(folder, "执照英文版.pdf")
    _touch(folder, "id-正面.jpg")
    _touch(folder, "id-反面.jpg")
    assert pipeline._folder_ready_to_move(folder)


def test_recover_ready_folders(tmp_path):
    src = tmp_path / "中国执照库"
    dest = tmp_path / "资料"
    src.mkdir()
    dest.mkdir()
    ready = src / "已译公司"
    ready.mkdir()
    _touch(ready, "cn_meta.json")
    _touch(ready, "a英文版.pdf")
    _touch(ready, "id-正面.jpg")
    _touch(ready, "id-反面.jpg")
    pending = src / "未译公司"
    pending.mkdir()
    _touch(pending, "photo.jpg")
    n = pipeline._recover_ready_folders(src, dest)
    assert n == 1
    assert (dest / "已译公司").is_dir()
    assert not ready.exists()
    assert pending.exists()


def test_recover_legacy_without_cn_meta_stays(tmp_path):
    """旧版失败稿也有英文 PDF+身份证, 没有 cn_meta 不能当成功补搬."""
    src = tmp_path / "中国执照库"
    dest = tmp_path / "资料"
    src.mkdir()
    dest.mkdir()
    ready = src / "旧失败"
    ready.mkdir()
    _touch(ready, "执照英文版.pdf")
    _touch(ready, "photo-正面.jpg")
    _touch(ready, "photo-反面.jpg")
    n = pipeline._recover_ready_folders(src, dest)
    assert n == 0
    assert ready.exists()


def test_recover_legacy_dual_pdf_keeps_both_without_named_meta(tmp_path):
    src = tmp_path / "中国执照库"
    dest = tmp_path / "资料"
    src.mkdir()
    dest.mkdir()
    ready = src / "旧双份"
    ready.mkdir()
    (ready / "cn_meta.json").write_text(
        '{"name_zh":"旧双份"}', encoding="utf-8")
    _touch(ready, "a英文版.pdf")
    _touch(ready, "b英文版.pdf")
    _touch(ready, "id-正面.jpg")
    _touch(ready, "id-反面.jpg")
    n = pipeline._recover_ready_folders(src, dest)
    assert n == 1
    moved = dest / "旧双份"
    assert (moved / "a英文版.pdf").exists()
    assert (moved / "b英文版.pdf").exists()


def test_recover_purges_extra_en_pdf(tmp_path):
    src = tmp_path / "中国执照库"
    dest = tmp_path / "资料"
    src.mkdir()
    dest.mkdir()
    ready = src / "已译公司"
    ready.mkdir()
    (ready / "cn_meta.json").write_text(
        '{"license_pdf":"good英文版.pdf"}', encoding="utf-8")
    _touch(ready, "good英文版.pdf")
    _touch(ready, "fail英文版.pdf")
    _touch(ready, "id-正面.jpg")
    _touch(ready, "id-反面.jpg")
    n = pipeline._recover_ready_folders(src, dest)
    assert n == 1
    moved = dest / "已译公司"
    assert (moved / "good英文版.pdf").exists()
    assert not (moved / "fail英文版.pdf").exists()


def test_license_library_counts(tmp_path):
    src = tmp_path / "中国执照库"
    src.mkdir()
    stuck = src / "卡住"
    stuck.mkdir()
    _touch(stuck, "cn_meta.json")
    _touch(stuck, "x英文版.pdf")
    _touch(stuck, "id-正面.jpg")
    _touch(stuck, "id-反面.jpg")
    pending = src / "待译"
    pending.mkdir()
    _touch(pending, "photo.jpg")
    (src / "资料不全").mkdir()
    p, s = pipeline.license_library_counts(src)
    assert p == 1
    assert s == 1


def test_move_company_folder(tmp_path):
    src = tmp_path / "源" / "公司A"
    dest = tmp_path / "资料"
    src.mkdir(parents=True)
    _touch(src, "a.txt")
    target = pipeline._move_company_folder(src, dest)
    assert target == dest / "公司A"
    assert (target / "a.txt").exists()
    assert not src.exists()
