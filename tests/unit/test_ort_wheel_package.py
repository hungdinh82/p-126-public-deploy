import zipfile

import pytest

from scripts import package_ort_wheel


@pytest.mark.parametrize("stale_archive", [False, True])
def test_incremental_wheel_uses_compiled_libraries_and_rejects_stale_archive(tmp_path, monkeypatch, stale_archive):
    build = tmp_path / "Release"
    stage = build / "onnxruntime/capi"
    previous = build / "build/lib/onnxruntime/capi"
    stage.mkdir(parents=True)
    previous.mkdir(parents=True)
    libraries = ("libonnxruntime_providers_tensorrt.so", "onnxruntime_pybind11_state.so")
    for name in libraries:
        (build / name).write_bytes(b"new compiled binary")
        (stage / name).write_bytes(b"stale staging binary")
        (previous / name).write_bytes(b"stale setuptools binary")

    def pack(*args, **kwargs):
        for name in libraries:
            assert (stage / name).read_bytes() == b"new compiled binary"
            assert (previous / name).read_bytes() == b"new compiled binary"
        dist = build / "dist"
        dist.mkdir()
        with zipfile.ZipFile(dist / "onnxruntime_gpu-1.20.1-cp312-cp312-linux_aarch64.whl", "w") as archive:
            for name in libraries:
                archive.writestr("onnxruntime/capi/" + name, b"stale" if stale_archive else (stage / name).read_bytes())

    monkeypatch.setattr(package_ort_wheel.subprocess, "run", pack)
    if stale_archive:
        with pytest.raises(RuntimeError, match="stale native library"):
            package_ort_wheel.package(build, tmp_path / "setup.py")
    else:
        package_ort_wheel.package(build, tmp_path / "setup.py")
