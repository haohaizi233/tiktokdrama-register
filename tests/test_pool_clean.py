# -*- coding: utf-8 -*-
"""号池邮箱导入清洗与格式校验测试。"""
import json
from pathlib import Path

import pool


def test_parse_lines_strips_quotes(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "POOL_DIR", tmp_path)
    # 带引号的邮箱应被剥离
    text = '1\t"LizaGoodlet519379@outlook.com----pw----cid----token\n' \
           '2\tRoyCornetta5068122@outlook.com----pw----cid----token\n' \
           '3\t"PolkLandell3564570@outlook.com"----pw----cid----token\n' \
           '4\tbad@email----pw----cid----token\n'  # 不合法
    parsed, bad = pool.parse_lines(text)
    emails = [p[0] for p in parsed]
    assert "LizaGoodlet519379@outlook.com" in emails
    assert "RoyCornetta5068122@outlook.com" in emails
    assert "PolkLandell3564570@outlook.com" in emails
    assert "bad@email" not in emails
    assert len(bad) == 1


def test_email_rejects_punctuation():
    assert not pool.EMAIL_RE.match('"test@outlook.com')
    assert not pool.EMAIL_RE.match("test@outlook,com")
    assert not pool.EMAIL_RE.match("te st@outlook.com")
    assert pool.EMAIL_RE.match("test@outlook.com")
    assert pool.EMAIL_RE.match("Test.Name+tag@sub.domain.org")


def test_clean_pool_fixes_dirty(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "POOL_DIR", tmp_path)
    monkeypatch.setattr(pool, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(pool, "LOG_DIR", tmp_path / "logs")
    # 写入脏数据
    f = tmp_path / "pool_test.json"
    f.write_text(json.dumps({
        "accounts": [
            {"email": '"dirty@outlook.com', "index": 1},
            {"email": "clean@outlook.com", "index": 2},
            {"email": '"bad@', "index": 3},  # 剥离后仍不合法
        ]
    }), encoding="utf-8")
    fixed, dropped = pool.clean_pool()
    assert fixed == 1
    assert dropped == 1
    accs = pool.all_accounts()
    emails = [a["email"] for a in accs]
    assert "dirty@outlook.com" in emails
    assert "clean@outlook.com" in emails
    assert '"bad@' not in emails


def test_scan_dirty(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "POOL_DIR", tmp_path)
    f = tmp_path / "pool_test.json"
    f.write_text(json.dumps({
        "accounts": [
            {"email": '"fixme@outlook.com', "index": 1},
            {"email": "ok@outlook.com", "index": 2},
            {"email": '"bad@', "index": 3},
        ]
    }), encoding="utf-8")
    fixable, droppable = pool.scan_dirty()
    assert '"fixme@outlook.com' in fixable
    assert '"bad@' in droppable
    assert "ok@outlook.com" not in fixable
    assert "ok@outlook.com" not in droppable
    monkeypatch.setattr(pool, "POOL_DIR", tmp_path)
    f = tmp_path / "pool_test.json"
    f.write_text(json.dumps({
        "accounts": [
            {"email": '"skipme@outlook.com', "index": 1},
            {"email": "ok@outlook.com", "index": 2},
        ]
    }), encoding="utf-8")
    accs = pool.all_accounts()
    emails = [a["email"] for a in accs]
    assert "ok@outlook.com" in emails
    assert '"skipme@outlook.com' not in emails


def test_strip_unicode_and_fullwidth_quotes():
    raw = "\u201cfoo@outlook.com\u201d"
    assert pool.EMAIL_RE.match(raw.strip(pool._STRIP_CHARS))
    raw2 = "\uff02bar@outlook.com\uff02"
    assert pool.EMAIL_RE.match(raw2.strip(pool._STRIP_CHARS))


def test_clean_pool_fixes_unicode_quotes(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "POOL_DIR", tmp_path)
    monkeypatch.setattr(pool, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(pool, "LOG_DIR", tmp_path / "logs")
    f = tmp_path / "pool_test.json"
    f.write_text(json.dumps({
        "accounts": [
            {"email": "\u201cuni@outlook.com\u201d", "index": 1, "status": "unused"},
        ]
    }), encoding="utf-8")
    fixed, dropped = pool.clean_pool()
    assert fixed == 1
    assert dropped == 0
    assert pool.all_accounts()[0]["email"] == "uni@outlook.com"


def test_all_accounts_defaults_missing_status(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "POOL_DIR", tmp_path)
    f = tmp_path / "pool_test.json"
    f.write_text(json.dumps({
        "accounts": [{"email": "a@outlook.com", "index": 1}]
    }), encoding="utf-8")
    accs = pool.all_accounts()
    assert accs[0]["status"] == "unused"


def test_update_account_matches_quoted_email(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "POOL_DIR", tmp_path)
    f = tmp_path / "pool_test.json"
    f.write_text(json.dumps({
        "accounts": [
            {"email": '"dirty@outlook.com', "index": 1, "status": "unused"},
        ]
    }), encoding="utf-8")
    pool.update_account("dirty@outlook.com", status="registered",
                        cert_status="submitted", institution="Lab")
    data = json.loads(f.read_text(encoding="utf-8"))
    a = data["accounts"][0]
    assert a["email"] == "dirty@outlook.com"
    assert a["status"] == "registered"
    assert a["cert_status"] == "submitted"
    assert a["institution"] == "Lab"


def test_clean_pool_relinks_usage_and_institutions(tmp_path, monkeypatch):
    pool_dir = tmp_path / "pool"
    mat_dir = tmp_path / "mat"
    pool_dir.mkdir()
    mat_dir.mkdir()
    monkeypatch.setattr(pool, "POOL_DIR", pool_dir)
    monkeypatch.setattr(pool, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(pool, "LOG_DIR", tmp_path / "logs")
    import institutions
    import materials
    inst_path = tmp_path / "institutions.json"
    monkeypatch.setattr(materials, "MATERIALS_DIR", mat_dir)
    monkeypatch.setattr(materials, "_slots_file", lambda: tmp_path / "company_slots.json")
    monkeypatch.setattr(institutions, "INST_FILE", inst_path)
    (pool_dir / "pool_test.json").write_text(json.dumps({
        "accounts": [
            {"email": '"a@outlook.com', "index": 1, "status": "unused"},
        ]
    }), encoding="utf-8")
    (mat_dir / "usage.json").write_text(json.dumps({
        "某公司": [{"email": '"a@outlook.com', "status": "assigned", "at": "t", "note": ""}]
    }), encoding="utf-8")
    inst_path.write_text(json.dumps([
        {"name": "Lab", "status": "assigned", "email": '"a@outlook.com'}
    ]), encoding="utf-8")
    pool.clean_pool()
    usage = json.loads((mat_dir / "usage.json").read_text(encoding="utf-8"))
    inst = json.loads(inst_path.read_text(encoding="utf-8"))
    assert usage["某公司"][0]["email"] == "a@outlook.com"
    assert inst[0]["email"] == "a@outlook.com"
    assert materials.find_open_slot("a@outlook.com")[0] == "某公司"
    assert institutions.find_open("a@outlook.com")["name"] == "Lab"
