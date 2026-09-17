"""GPU detection must finish quickly and must not import torch."""
from unittest.mock import patch
import subprocess

from license_gui import gpu_label_from_providers, probe_onnx_providers


def test_detect_gpu_status_skips_torch():
    fake_providers = ["CPUExecutionProvider", "CUDAExecutionProvider"]
    with patch("license_trans.ocr._onnx_providers", return_value=fake_providers):
        import sys
        sys.modules.pop("torch", None)
        from license_trans.ocr import detect_gpu_status
        info = detect_gpu_status(probe_torch=False, timeout=1)
    assert info["provider"] == "CUDA"
    assert info["torch_cuda"] is False
    assert "Selected: CUDA" in info["detail"]


def test_detect_gpu_status_cpu_when_no_ep():
    with patch("license_trans.ocr._onnx_providers", return_value=["CPUExecutionProvider"]):
        from license_trans.ocr import detect_gpu_status
        info = detect_gpu_status(probe_torch=False, timeout=1)
    assert info["provider"] == "CPU"


def test_gpu_label_cuda():
    text, color, detail = gpu_label_from_providers(
        ["CPUExecutionProvider", "CUDAExecutionProvider"])
    assert text == "GPU: CUDA ✓"
    assert "CUDA" in detail
    assert color.startswith("#")


def test_gpu_label_directml():
    text, _, detail = gpu_label_from_providers(
        ["CPUExecutionProvider", "DmlExecutionProvider"])
    assert "DirectML" in text
    assert "Selected: DirectML" in detail


def test_gpu_label_cpu():
    text, _, detail = gpu_label_from_providers(["CPUExecutionProvider"])
    assert "CPU" in text
    assert "Selected: CPU" in detail


def test_probe_onnx_timeout_returns_cpu():
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="python", timeout=0.1)

    with patch("license_gui.subprocess.run", side_effect=boom):
        info = probe_onnx_providers(timeout=0.1)
    assert info["ok"] is False
    assert info["providers"] == ["CPUExecutionProvider"]
    assert "timed out" in info["error"]


def test_probe_onnx_parses_child_json():
    class Fake:
        stdout = '{"ok": true, "providers": ["CUDAExecutionProvider"]}\n'
        stderr = ""

    with patch("license_gui.subprocess.run", return_value=Fake()):
        info = probe_onnx_providers(timeout=1)
    assert info["ok"] is True
    assert "CUDAExecutionProvider" in info["providers"]
