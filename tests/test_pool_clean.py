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
