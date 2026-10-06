from __future__ import annotations

import logging
import os
import platform
import re
import resource
import select
import shutil
import subprocess
import threading
import time
from collections import deque

from fastapi import APIRouter

from src.vivi.api.runtime import runtime

router = APIRouter(prefix="/api/v1")
logger = logging.getLogger(__name__)
_started_at = time.monotonic()
_gpu_samples: deque[tuple[float, list[dict]]] = deque(maxlen=40)
_gpu_lock = threading.Lock()
_gpu_stop = threading.Event()
_gpu_thread: threading.Thread | None = None


def _memory() -> dict:
    try:
        values = {}
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, value = line.split(":", 1)
                values[key] = int(value.split()[0]) * 1024
        total = values["MemTotal"]
        available = values.get("MemAvailable", values.get("MemFree", 0))
        used = total - available
        return {"total_bytes": total, "used_bytes": used, "free_bytes": available, "percent": round(used / total * 100, 1)}
    except (OSError, KeyError, ValueError):
        return {"total_bytes": None, "used_bytes": None, "free_bytes": None, "percent": None}


def _query_gpu() -> list[dict]:
    try:
        binary = shutil.which("nvidia-smi") or "/usr/lib/wsl/lib/nvidia-smi"
        result = subprocess.run(
            [binary, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=1, check=True,
        )
        devices = []
        for line in result.stdout.splitlines():
            name, usage, used, total, temperature = [part.strip() for part in line.split(",")]
            devices.append({"name": name, "usage_percent": float(usage), "memory_used_mb": float(used), "memory_total_mb": float(total), "temperature_c": float(temperature)})
        return devices
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _parse_tegrastats(line: str) -> list[dict]:
    """Convert Jetson GR3D_FREQ telemetry to the dashboard's GPU schema."""
    # Xavier reports GR3D_FREQ as ``X%@Y``; accept decimal or N/A values too.
    usage = re.search(r"\bGR3D_FREQ\s+(\d+(?:\.\d+)?)%", line)
    if usage is None:
        return []
    temperature = re.search(r"\bGPU@([\d.]+)C\b", line)
    return [
        {
            "name": "NVIDIA Jetson integrated GPU",
            "usage_percent": float(usage.group(1)),
            # Jetson uses shared system memory, not separately reported VRAM.
            "memory_used_mb": None,
            "memory_total_mb": None,
            "temperature_c": float(temperature.group(1)) if temperature else None,
        }
    ]


def _record_gpu_sample(sample: list[dict]) -> None:
    with _gpu_lock:
        _gpu_samples.append((time.time(), sample))


def _sample_tegrastats(binary: str) -> None:
    while not _gpu_stop.is_set():
        process = None
        try:
            process = subprocess.Popen(
                [binary, "--interval", "500"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            if process.stdout is None:
                raise OSError("tegrastats stdout pipe was not opened")
            while not _gpu_stop.is_set() and process.poll() is None:
                ready, _, _ = select.select([process.stdout], [], [], 0.75)
                if ready:
                    line = process.stdout.readline()
                    if not line:
                        break
                    _record_gpu_sample(_parse_tegrastats(line))
                else:
                    _record_gpu_sample([])
        except (OSError, ValueError, subprocess.SubprocessError):
            logger.exception("Jetson tegrastats GPU sampling failed")
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        _gpu_stop.wait(1)


def _sample_gpu() -> None:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi and os.path.isfile("/usr/lib/wsl/lib/nvidia-smi"):
        nvidia_smi = "/usr/lib/wsl/lib/nvidia-smi"
    tegrastats = os.environ.get("VIVI_TEGRASTATS_BINARY") or shutil.which("tegrastats")
    # Jetson normally has tegrastats but no nvidia-smi. If nvidia-smi is
    # installed but cannot query a GPU, try tegrastats once before settling on
    # the NVIDIA sampler (which remains useful for transient driver errors).
    if tegrastats and (not nvidia_smi or not _query_gpu()):
        _sample_tegrastats(tegrastats)
        return

    consecutive_errors = 0
    while not _gpu_stop.is_set():
        try:
            sample = _query_gpu()
            if consecutive_errors:
                logger.info("GPU metrics sampler recovered")
                consecutive_errors = 0
        except Exception:
            consecutive_errors += 1
            if consecutive_errors == 1 or consecutive_errors % 20 == 0:
                logger.exception("GPU metrics sampling failed (%s consecutive errors)", consecutive_errors)
            sample = []
        _record_gpu_sample(sample)
        _gpu_stop.wait(0.5)


def start_gpu_sampler() -> None:
    global _gpu_thread
    if _gpu_thread is not None and _gpu_thread.is_alive():
        return
    _gpu_stop.clear()
    _gpu_thread = threading.Thread(target=_sample_gpu, name="vivi-gpu-metrics", daemon=True)
    _gpu_thread.start()


def stop_gpu_sampler() -> None:
    _gpu_stop.set()
    if _gpu_thread is not None:
        _gpu_thread.join(timeout=1.5)


def _gpu() -> dict:
    now = time.time()
    with _gpu_lock:
        samples = [sample for timestamp, sample in _gpu_samples if now - timestamp <= 20 and sample]
    if not samples:
        samples = [_query_gpu()]
    devices = []
    for index, device in enumerate(samples[-1] if samples else []):
        matching = [sample[index] for sample in samples if index < len(sample)]
        devices.append({
            **device,
            "peak_usage_percent_20s": max((item["usage_percent"] for item in matching), default=device["usage_percent"]),
            "sampled_at": now,
        })
    source = "tegrastats" if devices and "Jetson" in devices[0].get("name", "") else "nvidia-smi" if devices else None
    return {"available": bool(devices), "source": source, "sample_interval_ms": 500, "devices": devices}


def _device(gpu: dict) -> dict:
    """Describe the detected compute hardware without changing pipeline config."""
    model = ""
    for path in ("/proc/device-tree/model",):
        try:
            with open(path, "rb") as handle:
                model = handle.read().replace(b"\x00", b"").decode("utf-8", errors="replace").strip()
            if model:
                break
        except OSError:
            continue
    devices = gpu.get("devices", [])
    gpu_name = devices[0].get("name", "") if devices else ""
    if model or os.path.exists("/etc/nv_tegra_release") or gpu.get("source") == "tegrastats":
        return {"kind": "jetson", "name": model or gpu_name or "NVIDIA Jetson", "gpu_backend": "tegrastats" if gpu.get("source") == "tegrastats" or shutil.which("tegrastats") else None}
    if gpu_name:
        return {"kind": "nvidia_rtx" if "RTX" in gpu_name.upper() else "nvidia_gpu", "name": gpu_name, "gpu_backend": "nvidia-smi"}
    return {"kind": "cpu", "name": platform.machine() or "CPU", "gpu_backend": None}


def _pipeline(events: list[dict]) -> dict:
    event = next((entry for entry in reversed(events) if entry.get("event_type") == "turn"), None)
    if not event:
        return {"status": "chưa có lượt chạy", "command": "", "session_id": None, "turn_id": None, "timings_ms": {}, "received_at": None, "output_at": None, "end_to_end_ms": None}
    return {"status": event.get("status", "unknown"), "command": event.get("transcript", ""), "session_id": event.get("session_id"), "turn_id": event.get("turn_id"), "timings_ms": event.get("latency_ms", {}), "received_at": event.get("received_at"), "output_at": event.get("output_at"), "end_to_end_ms": event.get("end_to_end_ms"), "input_to_output_ms": event.get("input_to_output_ms"), "input_to_audio_ready_ms": event.get("input_to_audio_ready_ms")}


def _components() -> dict[str, dict[str, str]]:
    stt_available, stt_detail = runtime.stt.availability()
    tts_available, tts_detail = runtime.tts.availability()
    providers = sorted(runtime.orchestrator.graph_providers)
    vehicle_available = runtime.vehicle.is_connected()
    handbook_available = runtime.settings.rag_handbook_db.is_file()
    return {
        "orchestration": {
            "status": "ready" if providers else "offline",
            "detail": ", ".join(providers) or "No configured providers",
        },
        "stt": {
            "status": "ready" if stt_available else "offline",
            "detail": f"{runtime.stt.name}: {stt_detail}",
        },
        "tts": {
            "status": "ready" if tts_available else "offline",
            "detail": f"{runtime.tts.name}: {tts_detail}",
        },
        "vehicle": {
            "status": "ready" if vehicle_available else "offline",
            "detail": runtime.vehicle.name,
        },
        "rag": {
            "status": "ready" if handbook_available else "offline",
            "detail": runtime.settings.rag_retrieval_mode,
        },
    }


@router.get("/metrics")
def metrics():
    events, tts_by_turn, last_tts = runtime.store.metrics_snapshot()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    disk = shutil.disk_usage(".")
    gpu = _gpu()
    return {
        "timestamp": time.time(),
        "uptime_seconds": round(time.monotonic() - _started_at, 1),
        "host": {"platform": platform.platform(), "cpu_count": os.cpu_count() or 1, "load_1m": round(os.getloadavg()[0], 2) if hasattr(os, "getloadavg") else None},
        "memory": _memory(),
        "process": {"rss_bytes": usage.ru_maxrss * 1024, "user_seconds": round(usage.ru_utime, 3), "system_seconds": round(usage.ru_stime, 3)},
        "disk": {"free_bytes": disk.free, "percent": round(disk.used / disk.total * 100, 1)},
        "gpu": gpu,
        "device": _device(gpu),
        "components": _components(),
        "storage": {"transcripts": runtime.settings.store_transcripts},
        "pipeline": _pipeline(events),
        "history": [
            {
                "session_id": event.get("session_id"),
                "turn_id": event.get("turn_id"),
                "command": event.get("transcript", ""),
                "status": event.get("status", "unknown"),
                "received_at": event.get("received_at"),
                "output_at": event.get("output_at"),
                "end_to_end_ms": event.get("end_to_end_ms"),
                "input_to_output_ms": event.get("input_to_output_ms"),
                "input_to_audio_ready_ms": event.get("input_to_audio_ready_ms"),
                "timings_ms": event.get("latency_ms", {}),
                "stt_ms": next((speech.get("latency_ms") for speech in reversed(events) if speech.get("event_type") == "stt" and speech.get("session_id") == event.get("session_id") and speech.get("turn_id") == event.get("turn_id")), None),
                "tts": tts_by_turn.get((event.get("session_id", ""), event.get("turn_id", "")), []),
            }
            for event in events
            if event.get("event_type") == "turn" and event.get("transcript")
        ],
        "tts": last_tts or {"time_to_first_chunk_ms": None, "synthesis_ms": None, "audio_duration_ms": None},
    }
