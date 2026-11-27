"""NVIDIA/CPU metrics via nvidia-smi (no extra deps)."""

from __future__ import annotations

import re
import subprocess


def _smi(args: list[str], timeout: float = 2.5) -> str:
    r = subprocess.run(
        ["nvidia-smi", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr or "nvidia-smi failed")
    return r.stdout


def has_nvidia() -> bool:
    try:
        _smi(["--query-gpu=name", "--format=csv,noheader"])
        return True
    except Exception:
        return False


def gpu_name() -> str | None:
    try:
        return _smi(["--query-gpu=name", "--format=csv,noheader"]).strip().splitlines()[0]
    except Exception:
        return None


def nvidia_series_ok(name: str | None) -> bool:
    """True for RTX 20xx and newer (default GPU use)."""
    if not name:
        return False
    n = name.upper()
    if "RTX" in n:
        m = re.search(r"RTX\s*(\d{2})", n)
        if m:
            return int(m.group(1)) >= 20
        # RTX 5090 style already matched; professional lines:
        if any(x in n for x in ("A100", "A10", "H100", "H200", "L40", "L4")):
            return True
    return False


def gpu_metrics() -> dict:
    """Return used/total MB, temp C, util %, power W for first GPU."""
    out = _smi(
        [
            "--query-gpu=memory.used,memory.total,temperature.gpu,utilization.gpu,power.draw",
            "--format=csv,noheader,nounits",
        ]
    ).strip().splitlines()[0]
    parts = [p.strip() for p in out.split(",")]
    used, total, temp, util, power = parts[:5]
    return {
        "used_mb": float(used),
        "total_mb": float(total),
        "temp": float(temp) if temp not in ("N/A", "[N/A]") else None,
        "util": float(util) if util not in ("N/A", "[N/A]") else None,
        "power_w": float(power) if power not in ("N/A", "[N/A]") else None,
    }


def cpu_ram() -> tuple[float | None, float | None]:
    """(used_percent, used_gb) via ctypes GlobalMemoryStatusEx — no psutil."""
    try:
        import ctypes
        from ctypes import wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        used_pct = float(stat.dwMemoryLoad)
        used_gb = (stat.ullTotalPhys - stat.ullAvailPhys) / 1024**3
        return used_pct, used_gb
    except Exception:
        return None, None


# rough VRAM if model is not loaded yet (CUDA float16)
VRAM_ESTIMATE_GB = {
    "tiny": 0.2,
    "base": 0.3,
    "small": 0.5,
    "medium": 1.5,
    "large-v3": 3.0,
    "large-v3-turbo": 1.6,
}


def cpu_load() -> float | None:
    """CPU load percent via GetSystemTimes (idle vs kernel+user)."""
    try:
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        def to_u64(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        idle = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        i = to_u64(idle)
        k = to_u64(kernel)
        u = to_u64(user)
        # kernel includes idle
        busy = (k + u) - i
        total = k + u
        if total <= 0:
            return None
        if not hasattr(cpu_load, "_prev"):
            cpu_load._prev = (i, total)
            return None
        pi, pt = cpu_load._prev
        cpu_load._prev = (i, total)
        dt = total - pt
        di = i - pi
        if dt <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (1.0 - di / dt)))
    except Exception:
        return None


def cpu_name() -> str:
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        )
        val, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        winreg.CloseKey(key)
        n = str(val).strip()
        if n:
            return n
    except Exception:
        pass
    try:
        import platform

        return platform.processor() or "CPU"
    except Exception:
        return "CPU"
