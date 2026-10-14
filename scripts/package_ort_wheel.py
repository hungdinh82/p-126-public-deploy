"""Refresh ORT's native wheel staging after an incremental CMake build."""

import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def digest(stream):
    value = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        value.update(chunk)
    return value.hexdigest()


def package(build: Path, setup: Path):
    stage = build / "onnxruntime/capi"
    if not stage.is_dir():
        raise FileNotFoundError("ORT Python staging directory missing")
    libraries = sorted([*build.glob("libonnxruntime_providers_*.so"),
                        *build.glob("onnxruntime_pybind11_state*.so")])
    if not libraries or not any("pybind11" in path.name for path in libraries):
        raise FileNotFoundError("Compiled ORT native libraries missing")
    for path in libraries:
        shutil.copy2(path, stage / path.name)
        # setuptools may otherwise keep a stale file with a newer timestamp.
        previous = build / "build/lib/onnxruntime/capi" / path.name
        if previous.parent.is_dir():
            shutil.copy2(path, previous)
    subprocess.run([sys.executable, str(setup.resolve()), "bdist_wheel", "--wheel_name_suffix=gpu"],
                   cwd=build, check=True)
    wheels = list((build / "dist").glob("onnxruntime_gpu-1.20.1-cp312-cp312-linux_aarch64.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Expected one native cp312/aarch64 ORT wheel")
    with zipfile.ZipFile(wheels[0]) as archive:
        for path in libraries:
            with path.open("rb") as native, archive.open("onnxruntime/capi/" + path.name) as packaged:
                if digest(native) != digest(packaged):
                    raise RuntimeError(f"Wheel contains stale native library: {path.name}")
    print(f"Verified native libraries in {wheels[0]}")


if __name__ == "__main__":
    package(Path(sys.argv[1]), Path(sys.argv[2]))
