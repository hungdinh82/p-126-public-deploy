"""Regression checks for setup metadata, original model recovery and cleanup."""

import ctypes
import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import check_jetson_runtime, prepare_ort_metadata, prepare_ort_platform, prepare_speech_models
from scripts.smoke_jetson_profile import stop


def test_smoke_cleanup_import_does_not_require_scipy():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            'import sys; sys.modules["scipy"] = None; '
            "from scripts.smoke_jetson_profile import stop; assert callable(stop)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_sdk_activation_includes_jetpack_cudla_without_interactive_shell():
    script = Path(__file__).resolve().parents[2] / "scripts/jetson_profile.sh"
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; jetson_activate_sdk; printf "%s" "$LD_LIBRARY_PATH"',
            "bash",
            str(script),
        ],
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
        check=True,
        capture_output=True,
        text=True,
    )
    libraries = result.stdout.split(":")
    assert "/usr/local/cuda-11.4/lib64" in libraries
    assert libraries.index("/usr/local/cuda-11.8/lib64") < libraries.index("/usr/local/cuda-11.4/lib64")


def test_eigen_metadata_changes_only_verified_dependency_and_preserves_backup(tmp_path):
    path = tmp_path / "deps.txt"
    original = f"# header\neigen;{prepare_ort_metadata.EIGEN_URL};{prepare_ort_metadata.OLD_SHA1}\nother;url;hash\n"
    path.write_text(original)
    prepare_ort_metadata.prepare(path)
    assert path.read_text() == original.replace(prepare_ort_metadata.OLD_SHA1, prepare_ort_metadata.VERIFIED_SHA1)
    assert path.with_name("deps.txt.before-eigen-checksum").read_text() == original
    first = path.read_bytes()
    prepare_ort_metadata.prepare(path)
    assert path.read_bytes() == first


def test_arm_cpu_bounds_fix_preserves_other_source_and_is_idempotent(tmp_path):
    path = tmp_path / "cpuid_info.cc"
    original = "// header\n" + prepare_ort_platform.ORIGINAL + "// footer\n"
    path.write_text(original)
    prepare_ort_platform.prepare(path)
    assert path.read_text() == "// header\n" + prepare_ort_platform.FIXED + "// footer\n"
    assert path.with_name(path.name + ".before-arm-cpuid-bounds").read_text() == original
    first = path.read_bytes()
    prepare_ort_platform.prepare(path)
    assert path.read_bytes() == first


def test_arm_cpu_bounds_fix_refuses_unexpected_source(tmp_path):
    path = tmp_path / "cpuid_info.cc"
    path.write_text("unrecognized source")
    with pytest.raises(ValueError, match="Unexpected"):
        prepare_ort_platform.prepare(path)
    assert path.read_text() == "unrecognized source"


@pytest.mark.parametrize("row", ["eigen;unexpected-url;hash", f"eigen;{prepare_ort_metadata.EIGEN_URL};unexpected"])
def test_eigen_metadata_refuses_unverified_inputs(tmp_path, row):
    path = tmp_path / "deps.txt"
    path.write_text(row)
    with pytest.raises(ValueError, match="Unexpected"):
        prepare_ort_metadata.prepare(path)
    assert path.read_text() == row


def test_recover_modified_tts_model_from_pinned_revision(tmp_path, monkeypatch):
    original = b"original model fixture"
    path = tmp_path / "zerotts/onnx/local_frame_decode.onnx"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"modified graph")
    monkeypatch.setattr(prepare_speech_models, "TTS_FRAME_SHA256", hashlib.sha256(original).hexdigest())
    calls = []

    def download(repo, **kwargs):
        calls.append((repo, kwargs))
        path.write_bytes(original)

    monkeypatch.setattr(prepare_speech_models, "hf_hub_download", download)
    prepare_speech_models.ensure_original_tts(tmp_path)
    assert path.read_bytes() == original
    assert calls[0][1]["revision"] == prepare_speech_models.TTS_REV
    assert calls[0][1]["force_download"] is True
    prepare_speech_models.ensure_original_tts(tmp_path)
    assert len(calls) == 1


def test_tts_bad_download_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "zerotts/onnx/local_frame_decode.onnx"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"bad archive")
    monkeypatch.setattr(prepare_speech_models, "hf_hub_download", lambda *a, **kw: None)
    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        prepare_speech_models.ensure_original_tts(tmp_path)


def test_smoke_cleanup_reaps_model_process():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        stop(process)
        assert process.poll() is not None
        stop(process)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def set_integer(pointer, value):
    ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int))[0] = value
    return 0


def test_gpu_preflight_rejects_wrong_cuda_runtime(monkeypatch):
    driver = SimpleNamespace(
        cuInit=lambda _: 0,
        cuDeviceGetCount=lambda p: set_integer(p, 1),
        cuDriverGetVersion=lambda p: set_integer(p, 11080),
        cuDeviceComputeCapability=lambda a, b, _: set_integer(a, 7) or set_integer(b, 2),
    )
    runtime = SimpleNamespace(cudaRuntimeGetVersion=lambda p: set_integer(p, 11040))
    monkeypatch.setattr(check_jetson_runtime.ctypes, "CDLL", lambda name: driver if name == "libcuda.so.1" else runtime)
    with pytest.raises(RuntimeError, match="runtime 11.8"):
        check_jetson_runtime.gpu_report()


def test_gpu_preflight_reports_driver_initialization_failure(monkeypatch):
    monkeypatch.setattr(check_jetson_runtime.ctypes, "CDLL", lambda _: SimpleNamespace(cuInit=lambda _: 803))
    with pytest.raises(RuntimeError, match="803"):
        check_jetson_runtime.gpu_report()
