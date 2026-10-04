from __future__ import annotations

import os
import platform
import resource
import shutil
import subprocess
import threading
import time
from collections import deque

from fastapi import APIRouter

from src.vivi.api.runtime import runtime

router = APIRouter(prefix="/api/v1")
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


def _sample_gpu() -> None:
    while not _gpu_stop.is_set():
        sample = _query_gpu()
        with _gpu_lock:
            _gpu_samples.append((time.time(), sample))
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
    return {"available": bool(devices), "sample_interval_ms": 500, "devices": devices}


def _pipeline() -> dict:
    event = next((entry for entry in reversed(runtime.store.recent_events) if entry.get("event_type") == "turn"), None)
    if not event:
        return {"status": "chưa có lượt chạy", "command": "", "session_id": None, "turn_id": None, "timings_ms": {}, "received_at": None, "output_at": None, "end_to_end_ms": None}
    return {"status": event.get("status", "unknown"), "command": event.get("transcript", ""), "session_id": event.get("session_id"), "turn_id": event.get("turn_id"), "timings_ms": event.get("latency_ms", {}), "received_at": event.get("received_at"), "output_at": event.get("output_at"), "end_to_end_ms": event.get("end_to_end_ms"), "input_to_output_ms": event.get("input_to_output_ms"), "input_to_audio_ready_ms": event.get("input_to_audio_ready_ms")}


@router.get("/metrics")
async def metrics():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    disk = shutil.disk_usage(".")
    return {
        "timestamp": time.time(),
        "uptime_seconds": round(time.monotonic() - _started_at, 1),
        "host": {"platform": platform.platform(), "cpu_count": os.cpu_count() or 1, "load_1m": round(os.getloadavg()[0], 2) if hasattr(os, "getloadavg") else None},
        "memory": _memory(),
        "process": {"rss_bytes": usage.ru_maxrss * 1024, "user_seconds": round(usage.ru_utime, 3), "system_seconds": round(usage.ru_stime, 3)},
        "disk": {"free_bytes": disk.free, "percent": round(disk.used / disk.total * 100, 1)},
        "gpu": _gpu(),
        "pipeline": _pipeline(),
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
                "stt_ms": next((speech.get("latency_ms") for speech in reversed(runtime.store.recent_events) if speech.get("event_type") == "stt" and speech.get("session_id") == event.get("session_id") and speech.get("turn_id") == event.get("turn_id")), None),
                "tts": runtime.store.tts_by_turn.get((event.get("session_id", ""), event.get("turn_id", "")), []),
            }
            for event in runtime.store.recent_events
            if event.get("event_type") == "turn" and event.get("transcript")
        ],
        "tts": runtime.store.last_tts or {"time_to_first_chunk_ms": None, "synthesis_ms": None, "audio_duration_ms": None},
    }
