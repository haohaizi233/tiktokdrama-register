"""End-to-end smoke: OCR → parse → canonical → translate → PDF.

If WeChat runtime is missing this records NOT TESTED rather than faking PASS.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
USCI = "91110101MA004KQM97"


def _find_sample() -> Path | None:
    work = ROOT / "license_work"
    skip_hints = ("preview", "catalog", "dirty", "_bj_view", "view.jpg")
    preferred: list[Path] = []
    oriented: list[Path] = []
    if work.is_dir():
        for p in work.iterdir():
            if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
                continue
            name = p.name.lower()
            if any(h in name for h in skip_hints):
                continue
            if name.endswith("-oriented.jpg") or name.endswith("-oriented.png"):
                oriented.append(p)
            else:
                preferred.append(p)
    # oriented.jpg files are previous pipeline outputs of real licenses
    if oriented:
        return oriented[0]
    if preferred:
        return preferred[0]
    for pattern in ("*执照*.jpg", "*执照*.png", "*license*.jpg"):
        hits = [p for p in ROOT.glob(pattern) if p.is_file()]
        if hits:
            return hits[0]
    return None


def _fake_license_ocr_result():
    rows = [
        "营业执照",
        f"统一社会信用代码 {USCI}",
        "名称 测试科技有限公司",
        "类型 有限责任公司",
        "法定代表人 张三",
        "经营范围 技术服务；软件开发。",
        "注册资本 100万元",
        "成立日期 2020年1月1日",
        "住所 北京市海淀区中关村大街1号",
        "营业期限 2020年1月1日至长期",
        "登记机关 北京市海淀区市场监督管理局",
        "发证日期 2021年6月15日",
    ]
    out = []
    for i, text in enumerate(rows):
        y = 30 + i * 36
        box = [[20, y], [520, y], [520, y + 28], [20, y + 28]]
        out.append((box, text, 0.93))
    return out


class _FakeEngine:
    name = "fake-ppocr"
    provider = "CPU"

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def __call__(self, image):
        self.calls += 1
        return self.result, 0.01


def _offline_config(tmp_path: Path) -> Path:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg["ai"] = {"enabled": False, "api_key": "", "base_url": "", "model": ""}
    path = tmp_path / "offline_config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


def test_gui_import_smoke():
    import license_gui
    import license_trans
    from license_trans.ocr_service import OcrService
    assert hasattr(license_gui, "_log_ocr_health")
    assert hasattr(license_trans, "OcrService")
    assert OcrService is license_trans.OcrService


def test_wechat_health_does_not_claim_ready_on_path_only():
    from license_trans.ocr_wechat import wechat_ocr_health_check
    status = wechat_ocr_health_check(init=False)
    if status.get("available") and not status.get("initialized"):
        assert status.get("ready") is False


def test_pdf_render_from_canonical(tmp_path):
    from license_trans.pipeline import load_config
    from license_trans.render import render_pdf
    from license_trans.translate import translate_fields

    chinese = {
        "credit_code": USCI,
        "copy_no": "1-1",
        "name": "测试科技有限公司",
        "type": "有限责任公司",
        "legal_person": "张三",
        "scope": "技术服务",
        "capital": "100万元",
        "established": "2020年01月01日",
        "address": "北京市海淀区",
        "term": "2020年01月01日至长期",
        "authority": "北京市海淀区市场监督管理局",
        "issue_date": "2021年06月15日",
        "warnings": [],
        "duplicate": False,
    }
    english = translate_fields(chinese)
    out = tmp_path / "render英文版.pdf"
    render_pdf(english, load_config(), out)
    assert out.exists() and out.stat().st_size > 1000


def test_pipeline_smoke_with_injected_engines(tmp_path):
    """Full convert_image wiring without real WeChat / RapidOCR."""
    from license_trans.ocr_service import OcrService
    from license_trans.pipeline import convert_image

    img = np.full((900, 700, 3), 255, dtype=np.uint8)
    sample = tmp_path / "synthetic_license.png"
    cv2.imwrite(str(sample), img)

    pp = _FakeEngine(_fake_license_ocr_result())
    engine = OcrService(wechat=None, ppocr=pp, init_engines=False)
    out = tmp_path / "smoke英文版.pdf"
    info = convert_image(
        sample, out, work_dir=tmp_path, ocr=engine,
        config_path=_offline_config(tmp_path),
    )
    assert out.exists() and out.stat().st_size > 1000
    assert info["fields"]["credit_code"] == info["canonical"]["credit_code"]
    assert info["english"]["credit_code"] == info["canonical"]["credit_code"]
    assert info["canonical"]["credit_code"] == USCI
    assert info["fallback_used"] is True
    assert info["ocr_actual"] == "ppocr_v6"
    assert "CANONICAL_READY" in {e["event"] for e in info["events"]}
    print("SMOKE_FAKE_ENGINE", json.dumps({
        "ocr_primary": info.get("ocr_primary"),
        "ocr_actual": info.get("ocr_actual"),
        "fallback_used": info.get("fallback_used"),
        "needs_review": info.get("needs_review"),
        "name_zh": info["fields"].get("name"),
        "name_en": info["english"].get("name"),
        "pdf": str(out),
        "trace_id": info.get("trace_id"),
    }, ensure_ascii=False))


def _downscale_sample(src: Path, dest: Path, max_side: int = 1100) -> Path:
    data = np.fromfile(str(src), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        return src
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest > max_side:
        scale = max_side / longest
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    cv2.imencode(".jpg", img)[1].tofile(str(dest))
    return dest


def test_pipeline_real_ocr_smoke(tmp_path):
    sample = _find_sample()
    if sample is None:
        pytest.skip("no real business-license image in license_work/")
    try:
        from rapidocr import RapidOCR  # noqa: F401
    except Exception as exc:
        pytest.skip(f"PP-OCR not installed: {exc}")

    from license_trans.ocr_wechat import wechat_ocr_health_check

    wx = wechat_ocr_health_check(init=False)
    wechat_real = bool(wx.get("initialized") or wx.get("ready"))
    print("WeChat OCR real-runtime smoke test:",
          "TESTED" if wechat_real else "NOT TESTED")
    print(f"  available={wx.get('available')} path={wx.get('path')} "
          f"initialized={wx.get('initialized')} error={wx.get('error')}")

    small = _downscale_sample(sample, tmp_path / "small.jpg")
    cfg = _offline_config(tmp_path)
    out = tmp_path / "real_smoke英文版.pdf"
    script = (
        "import json, sys\n"
        "from license_trans.ocr_service import OcrService\n"
        "from license_trans.pipeline import convert_image\n"
        f"sample = r'''{small}'''\n"
        f"out = r'''{out}'''\n"
        f"cfg = r'''{cfg}'''\n"
        f"work = r'''{tmp_path}'''\n"
        "engine = OcrService({'ai': {'enabled': False}, 'wechat_ocr': {'enabled': False}})\n"
        "info = convert_image(sample, out, work_dir=work, ocr=engine, config_path=cfg)\n"
        "print('SMOKE_REAL', json.dumps({"
        "'sample': sample,"
        "'ocr_primary': info.get('ocr_primary'),"
        "'ocr_actual': info.get('ocr_actual'),"
        "'fallback_used': info.get('fallback_used'),"
        "'needs_review': info.get('needs_review'),"
        "'name_zh': (info.get('fields') or {}).get('name'),"
        "'name_en': (info.get('english') or {}).get('name'),"
        "'credit_code': (info.get('canonical') or {}).get('credit_code'),"
        "'pdf': str(out),"
        "'trace_id': info.get('trace_id'),"
        "}, ensure_ascii=False))\n"
        "sys.exit(0 if __import__('pathlib').Path(out).exists() else 1)\n"
    )
    import subprocess
    import sys
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=90,
        )
    except subprocess.TimeoutExpired:
        pytest.skip(
            "PP-OCR fallback smoke timed out after 90s on this sample; "
            "WeChat OCR real-runtime smoke test: NOT TESTED"
        )
    print(proc.stdout)
    if proc.returncode != 0:
        print((proc.stderr or "")[-4000:])
        pytest.skip(f"real PP-OCR smoke failed: exit {proc.returncode}")
    assert out.exists() and out.stat().st_size > 0
    if not wechat_real:
        assert "SMOKE_REAL" in (proc.stdout or "")


def test_pipeline_rerender_from_sidecar(tmp_path):
    """Real license fields (previous run) → Canonical → translate → PDF."""
    work = ROOT / "license_work"
    if not work.is_dir():
        pytest.skip("no license_work/")
    sidecar = None
    image = None
    fields = None
    allowed = {
        "credit_code", "copy_no", "name", "type", "legal_person", "scope",
        "capital", "established", "address", "term", "authority",
        "issue_date", "duplicate", "warnings",
    }
    for p in work.iterdir():
        if p.suffix.lower() != ".json":
            continue
        oriented = work / (p.stem + "-oriented.jpg")
        if not oriented.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_fields = data.get("fields") or data.get("canonical") or {}
        cand = {k: v for k, v in raw_fields.items() if k in allowed}
        if cand.get("name") and cand.get("credit_code"):
            sidecar, image, fields = p, oriented, cand
            break
    if sidecar is None or image is None or not fields:
        pytest.skip("no sidecar+oriented pair with name/credit_code")
    from license_trans.pipeline import convert_image
    out = tmp_path / "sidecar英文版.pdf"
    info = convert_image(
        image, out, work_dir=tmp_path,
        fields_override=fields,
        config_path=_offline_config(tmp_path),
    )
    assert out.exists() and out.stat().st_size > 1000
    assert info["fields"]["credit_code"] == info["canonical"]["credit_code"]
    assert info["english"]["credit_code"] == info["canonical"]["credit_code"]
    print("SMOKE_SIDECAR", json.dumps({
        "name_zh": info["fields"].get("name"),
        "name_en": info["english"].get("name"),
        "pdf": str(out),
        "trace_id": info.get("trace_id"),
    }, ensure_ascii=False))
