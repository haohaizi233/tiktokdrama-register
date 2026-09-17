from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import cv2
import numpy as np

LOG = logging.getLogger("license_trans.ocr")

KEYWORDS = [
    "营业执照",
    "统一社会",
    "信用代码",
    "法定代表人",
    "经营范围",
    "注册资本",
    "成立日期",
    "登记机关",
]


@dataclass
class OcrLine:
    text: str
    score: float | None
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def to_dict(self) -> dict:
        return asdict(self)


def _read_image(path: str | Path) -> np.ndarray:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"Cannot read image (not a file): {path}")
    if p.stat().st_size == 0:
        raise ValueError(f"Cannot read image (empty file): {path}")
    data = np.fromfile(str(p), dtype=np.uint8)
    if data.size == 0:
        raise ValueError(f"Cannot read image (no data): {path}")
    try:
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    except cv2.error:
        raise ValueError(f"Cannot read image (corrupted or unsupported format): {path}")
    if img is None:
        raise ValueError(f"Cannot read image (decode returned None): {path}")
    return img


def _downscale(image: np.ndarray, max_side: int = 1600) -> np.ndarray:
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return image
    scale = max_side / longest
    return cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


def _ensure_min_size(image: np.ndarray, min_side: int = 1000) -> np.ndarray:
    """Upscale small images for better OCR accuracy on low-resolution photos."""
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest >= min_side:
        return image
    scale = min_side / longest
    return cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)


def _enhance(image: np.ndarray) -> np.ndarray:
    """CLAHE on the L channel of LAB for contrast/lighting normalization."""
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    enhanced = cv2.merge((l, a, b))
    return cv2.cvtColor(enhanced, cv2.COLOR_LAB2BGR)


def _enhance_and_clean(image: np.ndarray) -> np.ndarray:
    """Combined: remove red seal + CLAHE enhance in one pass (saves one OCR pass)."""
    cleaned = _remove_red_seal(image)
    enhanced = _enhance(cleaned)
    return enhanced


def _deskew(image: np.ndarray, max_angle: float = 12.0) -> np.ndarray:
    """Correct small skew angles by detecting text-line orientation."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    bw = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 15, 5)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 1))
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    angles: list[float] = []
    for c in contours:
        if cv2.contourArea(c) < 80:
            continue
        _, _, angle = cv2.minAreaRect(c)
        if angle < -45:
            angle += 90
        elif angle > 45:
            angle -= 90
        angles.append(angle)
    if not angles:
        return image
    median_angle = float(np.median(angles))
    if abs(median_angle) < 0.3 or abs(median_angle) > max_angle:
        return image
    h, w = image.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), median_angle, 1.0)
    return cv2.warpAffine(
        image, M, (w, h), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE)


def _correct_perspective(image: np.ndarray) -> np.ndarray:
    """Detect the license border quadrilateral and warp it flat."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(
        cv2.bitwise_not(binary), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]
    for c in contours:
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4 and cv2.contourArea(approx) > image.shape[0] * image.shape[1] * 0.2:
            pts = approx.reshape(4, 2).astype(np.float32)
            rect = _order_points(pts)
            w_max = max(
                np.linalg.norm(rect[0] - rect[1]),
                np.linalg.norm(rect[2] - rect[3]))
            h_max = max(
                np.linalg.norm(rect[0] - rect[3]),
                np.linalg.norm(rect[1] - rect[2]))
            if w_max < 100 or h_max < 100:
                continue
            dst = np.array(
                [[0, 0], [w_max - 1, 0], [w_max - 1, h_max - 1], [0, h_max - 1]],
                dtype=np.float32)
            M = cv2.getPerspectiveTransform(rect, dst)
            return cv2.warpPerspective(image, M, (int(w_max), int(h_max)))
    return image


def _order_points(pts: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


def _run_ocr(ocr, image: np.ndarray) -> list[OcrLine]:
    result, _elapse = ocr(image)
    lines: list[OcrLine] = []
    for box, text, score in result or []:
        text = (text or "").strip()
        if not text:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        conf: float | None
        if score is None:
            conf = None
        else:
            conf = float(score)
        lines.append(
            OcrLine(
                text=text,
                score=conf,
                x0=float(min(xs)),
                y0=float(min(ys)),
                x1=float(max(xs)),
                y1=float(max(ys)),
            )
        )
    return lines


def _keyword_hits(lines: list[OcrLine]) -> int:
    blob = "".join(line.text for line in lines)
    return sum(1 for key in KEYWORDS if key in blob)


_USCI_RE = re.compile(r"[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}")
_SCOPE_HEADS = ("一般项目", "许可项目", "一般经营项目", "许可经营项目")


def _order_score(lines: list[OcrLine]) -> int:
    """Reading-order check for orientation selection.

    Vertical-layout licenses OCR cleanly at both k=1 and k=3 (the 180°
    flip), because the per-line classifier un-flips each line — but only
    one of them puts labels above/left of their values. +1 per correct
    label→value pair, -1 per reversed pair.
    """
    credit_label = code_line = scope_label = scope_line = None
    for ln in lines:
        t = ln.text.replace(" ", "")
        if credit_label is None and "统一社会信用代码" in t and not _USCI_RE.search(t):
            credit_label = ln
        if code_line is None and _USCI_RE.search(t):
            code_line = ln
        if scope_label is None and t.startswith("经营范围") and len(t) <= 6:
            scope_label = ln
        if scope_line is None and any(h in t for h in _SCOPE_HEADS) and len(t) > 8:
            scope_line = ln
    score = 0
    for label, value in ((credit_label, code_line), (scope_label, scope_line)):
        if label is None or value is None or label is value:
            continue
        if value.y0 > label.y0 + 4 or (abs(value.y0 - label.y0) <= 8 and value.x0 > label.x1):
            score += 1
        elif value.y0 < label.y0 - 4 or (abs(value.y0 - label.y0) <= 8 and value.x0 < label.x0):
            score -= 1
    return score


def _remove_red_seal(image: np.ndarray) -> np.ndarray:
    """Replace red seal pixels with white to expose covered text."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask1 = cv2.inRange(hsv, np.array([0, 60, 40]), np.array([10, 255, 255]))
    mask2 = cv2.inRange(hsv, np.array([165, 60, 40]), np.array([180, 255, 255]))
    mask = cv2.bitwise_or(mask1, mask2)
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.dilate(mask, kernel, iterations=1)
    result = image.copy()
    result[mask > 0] = (255, 255, 255)
    return result


def _iou(a: OcrLine, b: OcrLine) -> float:
    ix0, iy0 = max(a.x0, b.x0), max(a.y0, b.y0)
    ix1, iy1 = min(a.x1, b.x1), min(a.y1, b.y1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    union = (a.x1 - a.x0) * (a.y1 - a.y0) + (b.x1 - b.x0) * (b.y1 - b.y0) - inter
    return inter / union if union > 0 else 0.0


def _usable_score(score: float | None) -> float | None:
    return score if isinstance(score, (int, float)) else None


def _merge_lines(primary: list[OcrLine], secondary: list[OcrLine], iou_thresh: float = 0.3) -> list[OcrLine]:
    """Merge two OCR line sets; for overlapping lines keep the higher-score one.

    A missing confidence (WeChat) never beats a real PP-OCR score.
    """
    result = list(primary)
    for sec in secondary:
        overlapped = False
        for i, pri in enumerate(result):
            if _iou(pri, sec) > iou_thresh:
                pri_s, sec_s = _usable_score(pri.score), _usable_score(sec.score)
                if sec_s is None:
                    if pri_s is None and len(sec.text) > len(pri.text):
                        result[i] = sec
                elif pri_s is None or (sec_s > pri_s and len(sec.text) >= len(pri.text)):
                    result[i] = sec
                overlapped = True
                break
        if not overlapped:
            result.append(sec)
    return result


# ---------------------------------------------------------------------------
# OCR model metadata (for logging, debugging, benchmarking)
# ---------------------------------------------------------------------------
OCR_BACKEND_NAME = "RapidOCR"
OCR_VERSION = "PP-OCRv6"
OCR_MODEL_TYPE = "medium"
OCR_ENGINE_TYPE = "onnxruntime"


_gpu_provider: str | None = None


def _onnx_providers(timeout: float = 5.0) -> list[str]:
    """List ONNX Runtime providers in a child process.

    Importing onnxruntime / probing CUDA EP in-process can hang on Windows.
    A subprocess timeout always returns so callers are never stuck.
    """
    script = (
        "import json,sys\n"
        "try:\n"
        "    import onnxruntime as ort\n"
        "    json.dump({'p': list(ort.get_available_providers())}, sys.stdout)\n"
        "except Exception as e:\n"
        "    json.dump({'p': ['CPUExecutionProvider'], 'e': str(e)}, sys.stdout)\n"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=flags,
        )
        data = json.loads((proc.stdout or "").strip() or "{}")
        providers = list(data.get("p") or ["CPUExecutionProvider"])
        return providers or ["CPUExecutionProvider"]
    except subprocess.TimeoutExpired:
        LOG.warning(
            "[OCR] onnxruntime provider probe timed out after %.1ss — using CPU",
            timeout,
        )
        return ["CPUExecutionProvider"]
    except Exception:
        LOG.exception("[OCR] onnxruntime provider probe failed")
        return ["CPUExecutionProvider"]


def detect_gpu_provider() -> str:
    """Detect available GPU for OCR.

    Checks ONNX Runtime providers (CUDA/DirectML) only. Does not import
    PyTorch — that import can hang for minutes on Windows and is not
    required by RapidOCR / PP-OCRv6.
    Returns "CUDA", "DirectML", or "CPU".
    """
    global _gpu_provider
    if _gpu_provider is not None:
        return _gpu_provider
    providers = _onnx_providers()
    if "CUDAExecutionProvider" in providers:
        _gpu_provider = "CUDA"
    elif "DmlExecutionProvider" in providers:
        _gpu_provider = "DirectML"
    else:
        _gpu_provider = "CPU"
    return _gpu_provider


# ---------------------------------------------------------------------------
# Pluggable OCR engine wrappers
# ---------------------------------------------------------------------------

class RapidOCREngine:
    """Default OCR engine — PP-OCRv6 Medium via ONNX Runtime.

    Uses the new ``rapidocr`` package (>=3.9.0) with explicit PP-OCRv6
    Medium detection and recognition models.  GPU is used automatically
    when a CUDA or DirectML provider is detected; otherwise falls back to
    CPU.  No silent downgrade to Small/PP-OCRv5/v4/v3 is ever performed.
    """

    name = "RapidOCR · PP-OCRv6 Medium · ONNX Runtime"

    def __init__(self):
        import logging
        log = logging.getLogger("license_trans.ocr")
        from rapidocr import RapidOCR, EngineType, ModelType, OCRVersion

        provider = detect_gpu_provider()
        log.info("[OCR] Initializing RapidOCR...")
        log.info("[OCR] Engine: ONNX Runtime")
        log.info("[OCR] Detection: PP-OCRv6 Medium")
        log.info("[OCR] Recognition: PP-OCRv6 Medium")

        params = {
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Det.model_type": ModelType.MEDIUM,
            "Det.ocr_version": OCRVersion.PPOCRV6,
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.model_type": ModelType.MEDIUM,
            "Rec.ocr_version": OCRVersion.PPOCRV6,
        }
        # Enable GPU provider when detected (new rapidocr handles this natively)
        if provider == "CUDA":
            params["Det.engine_cfg.use_cuda"] = True
            params["Rec.engine_cfg.use_cuda"] = True
            log.info("[OCR] GPU: CUDA")
        elif provider == "DirectML":
            params["Det.engine_cfg.use_dml"] = True
            params["Rec.engine_cfg.use_dml"] = True
            log.info("[OCR] GPU: DirectML")
        else:
            log.info("[OCR] GPU: CPU (no GPU provider detected)")

        try:
            self._ocr = RapidOCR(params=params)
        except Exception as exc:
            log.error("[OCR] PP-OCRv6 Medium initialization failed: %s", exc)
            raise

        log.info("[OCR] Initialization completed")
        self._provider = provider

    @property
    def provider(self) -> str:
        return self._provider

    def __call__(self, image: np.ndarray):
        """Run OCR and adapt RapidOCROutput → legacy (result, elapse) format.

        Returns ``([(box, text, score), ...], elapse)`` matching the old
        ``rapidocr_onnxruntime`` calling convention so that ``_run_ocr``
        and downstream modules need no changes.
        """
        output = self._ocr(image)
        # Adapter: RapidOCROutput → [(box, text, score), ...]
        if output is None:
            return [], 0.0
        boxes = output.boxes
        txts = output.txts
        scores = output.scores
        if boxes is None or txts is None or scores is None:
            return [], float(output.elapse or 0.0)
        result = []
        for box, text, score in zip(boxes, txts, scores):
            # box is np.ndarray shape (4, 2) — convert to list of [x, y]
            box_list = [[float(p[0]), float(p[1])] for p in box]
            result.append((box_list, text, float(score)))
        return result, float(output.elapse or 0.0)


class EasyOCREngine:
    """EasyOCR engine — larger models, native GPU support via PyTorch."""

    name = "EasyOCR"

    def __init__(self):
        import easyocr
        gpu = detect_gpu_provider() == "CUDA"
        self._gpu = gpu
        self._reader = easyocr.Reader(["ch_sim", "en"], gpu=gpu)

    @property
    def provider(self) -> str:
        return "CUDA" if self._gpu else "CPU"

    def __call__(self, image: np.ndarray):
        import time
        t0 = time.time()
        raw = self._reader.readtext(image)
        elapsed = time.time() - t0
        # EasyOCR returns (bbox, text, confidence) — same as RapidOCR
        results = [(item[0], item[1], float(item[2])) for item in raw]
        return results, elapsed


class PaddleOCREngine:
    """PaddleOCR engine — best Chinese OCR accuracy, GPU support."""

    name = "PaddleOCR"

    def __init__(self):
        from paddleocr import PaddleOCR
        gpu = detect_gpu_provider() == "CUDA"
        self._gpu = gpu
        self._ocr = PaddleOCR(use_angle_cls=True, lang="ch", use_gpu=gpu)

    @property
    def provider(self) -> str:
        return "CUDA" if self._gpu else "CPU"

    def __call__(self, image: np.ndarray):
        import time
        t0 = time.time()
        raw = self._ocr.ocr(image, cls=True)
        elapsed = time.time() - t0
        results = []
        for page in raw or []:
            for line in page or []:
                results.append((line[0], line[1][0], float(line[1][1])))
        return results, elapsed


def _patch_ort_session_for_legacy_gpu():
    """Monkey-patch legacy rapidocr_onnxruntime OrtInferSession for GPU."""
    provider = detect_gpu_provider()
    if provider == "CPU":
        return "CPU"
    if provider not in ("CUDA", "DirectML"):
        return "CPU"

    from rapidocr_onnxruntime.utils import OrtInferSession
    from onnxruntime import (
        GraphOptimizationLevel, InferenceSession, SessionOptions,
    )
    from pathlib import Path

    if provider == "CUDA":
        ep_name = "CUDAExecutionProvider"
        ep_opts = {
            "device_id": 0,
            "arena_extend_strategy": "kNextPowerOfTwo",
            "cudnn_conv_algo_search": "EXHAUSTIVE",
            "do_copy_in_default_stream": True,
        }
    else:
        ep_name = "DmlExecutionProvider"
        ep_opts = {
            "device_id": 0,
            "arena_extend_strategy": "kSameAsRequested",
        }

    cpu_ep = "CPUExecutionProvider"
    cpu_opts = {"arena_extend_strategy": "kSameAsRequested"}

    def _patched_init(self, config):
        sess_opt = SessionOptions()
        sess_opt.log_severity_level = 4
        sess_opt.enable_cpu_mem_arena = False
        sess_opt.graph_optimization_level = GraphOptimizationLevel.ORT_ENABLE_ALL
        model_path = Path(config["model_path"])
        if not model_path.exists():
            raise FileNotFoundError(f"{model_path} does not exist.")
        try:
            self.session = InferenceSession(
                str(model_path), sess_options=sess_opt,
                providers=[(ep_name, ep_opts), (cpu_ep, cpu_opts)])
        except Exception:
            LOG.exception("[OCR] GPU EP failed, falling back to CPU")
            self.session = InferenceSession(
                str(model_path), sess_options=sess_opt,
                providers=[(cpu_ep, cpu_opts)])

    OrtInferSession.__init__ = _patched_init
    return provider


class LegacyRapidOCREngine:
    """Original PP-OCRv3 engine via the old rapidocr_onnxruntime package.

    Faster on CPU than PP-OCRv6 Medium but less accurate.  Kept as a
    user-selectable fallback for slow machines or quick scans.
    """

    name = "RapidOCR · PP-OCRv3 (Legacy)"

    def __init__(self):
        import logging
        log = logging.getLogger("license_trans.ocr")
        from rapidocr_onnxruntime import RapidOCR
        log.info("[OCR] Initializing legacy RapidOCR (PP-OCRv3)...")
        self._provider = _patch_ort_session_for_legacy_gpu()
        self._ocr = RapidOCR()
        log.info("[OCR] Legacy initialization completed (provider: %s)", self._provider)

    @property
    def provider(self) -> str:
        return self._provider

    def __call__(self, image: np.ndarray):
        return self._ocr(image)


def detect_gpu_status(*, probe_torch: bool = False, timeout: float = 5.0) -> dict:
    """Detect GPU availability and return detailed status info.

    Skips PyTorch by default: ``import torch`` / ``torch.cuda.is_available()``
    frequently hangs on Windows and is unused by the PP-OCRv6 path.
    """
    global _gpu_provider
    providers = _onnx_providers(timeout=timeout)
    provider = "CPU"
    if "CUDAExecutionProvider" in providers:
        provider = "CUDA"
    elif "DmlExecutionProvider" in providers:
        provider = "DirectML"
    _gpu_provider = provider

    torch_cuda = False
    if probe_torch:
        try:
            import torch
            torch_cuda = bool(torch.cuda.is_available())
            if torch_cuda and provider == "CPU":
                provider = "CUDA"
                _gpu_provider = provider
        except Exception:
            LOG.exception("[OCR] PyTorch CUDA probe failed")

    parts = [f"ONNX Runtime providers: {', '.join(providers) or 'unavailable'}"]
    if probe_torch:
        parts.append(f"PyTorch CUDA: {'available' if torch_cuda else 'no'}")
    parts.append(f"Selected: {provider}")
    return {
        "provider": provider,
        "providers": providers,
        "torch_cuda": torch_cuda,
        "detail": "\n".join(parts),
    }


def _read_engine_config() -> dict:
    """Read OCR config from config.json.

    Returns a dict with:
      - "engine": ocr_engine value ("auto" / "rapidocr" / "easyocr" / "paddleocr")
      - "model": ocr_model value ("v6_medium" / "v3" / "auto")
    """
    defaults = {"engine": "auto", "model": "auto"}
    try:
        from .pipeline import load_config
        cfg = load_config()
        defaults["engine"] = cfg.get("ocr_engine", "auto")
        defaults["model"] = cfg.get("ocr_model", "auto")
    except Exception:
        LOG.exception("[OCR] failed to read engine config, using defaults")
    return defaults


def _create_engine(preferred: str | None = None, model: str | None = None):
    """Create the best available OCR engine.

    ``preferred`` overrides the engine choice (auto/rapidocr/easyocr/paddleocr).
    ``model`` overrides the model choice (auto/v6_medium/v3) — only meaningful
    when the engine is RapidOCR.

    Priority when engine='auto':
      1. PaddleOCR (if installed) — best Chinese accuracy
      2. EasyOCR (if installed + GPU) — larger models, GPU
      3. RapidOCR (always) — PP-OCRv6 Medium by default

    For RapidOCR, model='v3' selects the legacy PP-OCRv3 (faster on CPU);
    model='v6_medium' or 'auto' selects PP-OCRv6 Medium (more accurate).
    """
    cfg = _read_engine_config()
    choice = preferred or cfg["engine"]
    model_choice = model or cfg["model"]

    if choice in ("paddleocr", "paddle"):
        return PaddleOCREngine()
    if choice in ("easyocr", "easy"):
        return EasyOCREngine()
    if choice in ("rapidocr", "rapid"):
        if model_choice == "v3":
            return LegacyRapidOCREngine()
        return RapidOCREngine()

    # auto: try PaddleOCR → EasyOCR → RapidOCR
    try:
        return PaddleOCREngine()
    except ImportError:
        LOG.debug("PaddleOCR not installed, skipping")
    try:
        return EasyOCREngine()
    except ImportError:
        LOG.debug("EasyOCR not installed, skipping")
    if model_choice == "v3":
        return LegacyRapidOCREngine()
    return RapidOCREngine()


_ZOOM_ANCHOR_RE = re.compile(
    r"营业执照|统一社会|信用代码|法定代表人|经营范围|注册资本|注册资金|"
    r"成立日期|营业期限|经营期限|登记机关|发证日期|核准日期|住所|经营场所|"
    r"gsxt|监督管理总局|信用信息公示系统"
)
_ZOOM_USCI_RE = re.compile(r"[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}")
_ZOOM_DATE_RE = re.compile(r"20\d{2}\s*年")


def _zoom_crop_pass(
    image: np.ndarray,
    lines: list[OcrLine],
    ocr_engine,
    target_side: int = 1600,
) -> tuple[np.ndarray, list[OcrLine]] | None:
    """Re-OCR a handheld shot zoomed onto the license paper.

    Photos where a person holds the license show the paper as a small part
    of the frame; after full-frame downscale the small print (address,
    scope, authority) drops below the OCR's usable glyph size and only the
    large print survives. Cropping to the anchor bounding box and upscaling
    recovers those lines. Returns None when the license already fills the
    frame or the zoomed pass is not clearly better.
    """
    h, w = image.shape[:2]
    anchors = [
        ln for ln in lines
        if _ZOOM_ANCHOR_RE.search(ln.text) or _ZOOM_USCI_RE.search(ln.text)
        or _ZOOM_DATE_RE.search(ln.text)
    ]
    if len(anchors) < 6:
        return None
    x0 = min(a.x0 for a in anchors)
    x1 = max(a.x1 for a in anchors)
    y0 = min(a.y0 for a in anchors)
    y1 = max(a.y1 for a in anchors)
    bw, bh = x1 - x0, y1 - y0
    if bw < w * 0.15 or bh < h * 0.15:
        return None
    if bw * bh >= w * h * 0.55:
        return None
    m = int(0.10 * min(bw, bh))
    cx0, cy0 = max(0, int(x0 - m)), max(0, int(y0 - m))
    cx1, cy1 = min(w, int(x1 + m)), min(h, int(y1 + m))
    cw, ch = cx1 - cx0, cy1 - cy0
    scale = target_side / float(max(cw, ch))
    if scale < 1.6:
        return None
    crop = image[cy0:cy1, cx0:cx1]
    resized = cv2.resize(crop, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_CUBIC)
    zlines = _run_ocr(ocr_engine, resized)
    old_chars = sum(len(ln.text) for ln in lines)
    new_chars = sum(len(ln.text) for ln in zlines)
    if _keyword_hits(zlines) >= _keyword_hits(lines) and new_chars > old_chars * 1.25:
        return resized, zlines
    return None


class LicenseOCR:
    def __init__(self, engine_name: str | None = None,
                 model: str | None = None,
                 engine=None) -> None:
        self._engine = engine if engine is not None else _create_engine(engine_name, model)
        self._ocr = self._engine  # _run_ocr calls ocr(image)
        self.gpu_provider = getattr(self._engine, "provider", "CPU")
        self.engine_name = getattr(self._engine, "name", "RapidOCR")
        self._quick_cache: dict[str, list[OcrLine]] = {}
        import threading
        self._cache_lock = threading.Lock()

    def recognize(self, path: str | Path, *,
                  max_side: int = 1600,
                  meta: dict | None = None) -> tuple[np.ndarray, list[OcrLine]]:
        """OCR a license photo, trying rotations to find the readable layout.

        Returns (oriented_image, lines). When ``meta`` is provided it is
        filled with:
          - "rotation": chosen k (np.rot90 count applied to the source)
          - "vertical_native": True when the source photo shows a portrait
            license with vertical text columns (竖版执照). For those the k=1
            OCR view lays the paper on its side, so callers that want the
            document's natural upright look must rotate the result back.
        """
        image = _ensure_min_size(_downscale(_read_image(path), max_side=max_side))
        best_img = image
        best_lines: list[OcrLine] = []
        best_score = (-1, -1, -1)
        best_k = 0
        k0_lines: list[OcrLine] | None = None

        # Reuse cached quick_detect lines as the k=0 (unrotated) baseline
        key = str(Path(path).resolve())
        with self._cache_lock:
            cached_lines = self._quick_cache.pop(key, None)

        if cached_lines is not None:
            hits = _keyword_hits(cached_lines)
            horiz = sum(1 for ln in cached_lines if (ln.x1 - ln.x0) > (ln.y1 - ln.y0) * 1.2)
            best_score = (hits, horiz, _order_score(cached_lines), len(cached_lines))
            best_lines = cached_lines
            k0_lines = cached_lines
            # Keyword hits alone cannot confirm orientation: vertical-layout
            # licenses (columns read top-to-bottom) hit every keyword while
            # their scope text shatters into cross-column fragments. Only
            # skip rotation when the lines are also predominantly horizontal.
            mostly_horizontal = horiz >= len(cached_lines) * 0.5
            rotations = range(1, 4) if (hits < 5 or not mostly_horizontal) else range(0)
        else:
            rotations = range(4)

        for k in rotations:
            rotated = np.ascontiguousarray(np.rot90(image, k))
            lines = _run_ocr(self._ocr, rotated)
            if k == 0:
                k0_lines = lines
            hits = _keyword_hits(lines)
            horiz = sum(1 for line in lines if (line.x1 - line.x0) > (line.y1 - line.y0) * 1.2)
            order = _order_score(lines)
            score = (hits, horiz, order, len(lines))
            if score > best_score:
                best_score = score
                best_img = rotated
                best_lines = lines
                best_k = k
            if hits >= 5 and horiz >= len(lines) * 0.5 and order >= 0:
                break

        # Handheld-shot zoom: re-OCR the paper at readable size, keeping the
        # pre-zoom full view so callers can still find the QR code (which sits
        # outside the text anchor box in these photos).
        zoomed = _zoom_crop_pass(best_img, best_lines, self._ocr)
        if zoomed is not None:
            if meta is not None:
                meta["full_view"] = np.ascontiguousarray(best_img)
            best_img, best_lines = zoomed

        deskewed = _deskew(best_img)
        if deskewed.shape[:2] != best_img.shape[:2]:
            deskewed_lines = _run_ocr(self._ocr, deskewed)
            if _keyword_hits(deskewed_lines) >= _keyword_hits(best_lines):
                best_img = deskewed
                best_lines = deskewed_lines
        corrected = _correct_perspective(best_img)
        if corrected.shape[:2] != best_img.shape[:2]:
            corrected_lines = _run_ocr(self._ocr, corrected)
            if _keyword_hits(corrected_lines) >= _keyword_hits(best_lines):
                best_img = corrected
                best_lines = corrected_lines
        # Combined seal removal + CLAHE in one pass (saves one OCR pass)
        enhanced_clean = _enhance_and_clean(best_img)
        enhanced_clean_lines = _run_ocr(self._ocr, enhanced_clean)
        merged = _merge_lines(best_lines, enhanced_clean_lines)
        merged.sort(key=lambda line: (round(line.y0 / 18), line.x0))
        if meta is not None:
            meta["rotation"] = best_k
            vertical_native = False
            if k0_lines:
                k0_tall = sum(
                    1 for ln in k0_lines
                    if (ln.y1 - ln.y0) > (ln.x1 - ln.x0) * 1.5)
                vertical_native = (
                    _keyword_hits(k0_lines) >= 5 and k0_tall >= len(k0_lines) * 0.5)
            meta["vertical_native"] = vertical_native
        return best_img, merged

    def quick_detect(self, path: str | Path) -> tuple[bool, list[OcrLine]]:
        """Single-pass OCR for fast license detection.

        No rotation, deskew, perspective, seal removal, or CLAHE.
        Returns (is_license, lines) where is_license checks for '营业执照'.
        The lines are cached so recognize() can reuse them as the k=0 baseline,
        avoiding a redundant OCR pass on the unrotated image.
        """
        image = _ensure_min_size(_downscale(_read_image(path)))
        lines = _run_ocr(self._ocr, image)
        key = str(Path(path).resolve())
        with self._cache_lock:
            self._quick_cache[key] = lines
        blob = "".join(line.text for line in lines)
        compact = blob.replace(" ", "").replace("\n", "").replace("\u3000", "")
        return "营业执照" in compact, lines
