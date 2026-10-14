"""Lightweight Linux resident-memory readings; RSS is per process, not per model."""
from pathlib import Path

GIB = 1024 ** 3


def _kib_fields(path: Path) -> dict[str, int]:
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeError):
        return {}
    result = {}
    for line in lines:
        parts = line.split()
        if len(parts) == 3 and parts[2] == "kB" and parts[1].isdigit():
            result[parts[0].rstrip(":")] = int(parts[1]) * 1024
    return result


def memory_snapshot(models: dict, proc: Path = Path("/proc")) -> dict:
    """Enrich copied capability records; never attribute shared RSS to one model."""
    groups: dict[int, list[str]] = {}
    for name, model in models.items():
        try:
            pid = int(model.get("pid"))
        except (TypeError, ValueError):
            pid = 0
        model["pid"] = pid if pid > 0 else None
        if pid > 0:
            groups.setdefault(pid, []).append(name)
    for pid, names in groups.items():
        rss = _kib_fields(proc / str(pid) / "status").get("VmRSS")
        for name in names:
            models[name]["memory"] = {
                "rss_bytes": rss,
                "rss_gib": rss / GIB if rss is not None else None,
                "scope": "process", "shared_with": [n for n in names if n != name],
            }
    for model in models.values():
        model.setdefault("memory", {"rss_bytes": None, "rss_gib": None,
                                    "scope": "process", "shared_with": []})
    fields = _kib_fields(proc / "meminfo")
    total, free = fields.get("SwapTotal"), fields.get("SwapFree")
    return {"total_bytes": fields.get("MemTotal"),
            "available_bytes": fields.get("MemAvailable"),
            "swap_used_bytes": max(0, total - free) if total is not None and free is not None else None}
