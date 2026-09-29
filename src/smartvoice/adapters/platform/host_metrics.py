"""Small, dependency-free host and process resource snapshots."""

from __future__ import annotations

import os
import platform
import subprocess
import time
from functools import lru_cache
from typing import Any


def _windows_memory_status() -> dict[str, int] | None:
    if os.name != "nt":
        return None

    import ctypes
    from ctypes import wintypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return {
        "total_physical_bytes": int(status.ullTotalPhys),
        "available_physical_bytes": int(status.ullAvailPhys),
        "memory_load_percent": int(status.dwMemoryLoad),
    }


def _windows_process_memory() -> dict[str, int] | None:
    if os.name != "nt":
        return None

    import ctypes
    from ctypes import wintypes

    size_t = ctypes.c_size_t

    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", size_t), ("WorkingSetSize", size_t),
            ("QuotaPeakPagedPoolUsage", size_t), ("QuotaPagedPoolUsage", size_t),
            ("QuotaPeakNonPagedPoolUsage", size_t), ("QuotaNonPagedPoolUsage", size_t),
            ("PagefileUsage", size_t), ("PeakPagefileUsage", size_t), ("PrivateUsage", size_t),
        ]

    counters = ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("Kernel32.dll")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    process = kernel32.GetCurrentProcess()
    psapi = ctypes.WinDLL("Psapi.dll")
    get_memory_info = psapi.GetProcessMemoryInfo
    get_memory_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessMemoryCountersEx), wintypes.DWORD]
    get_memory_info.restype = wintypes.BOOL
    ok = get_memory_info(process, ctypes.byref(counters), counters.cb)
    if not ok:
        return None
    return {
        "working_set_bytes": int(counters.WorkingSetSize),
        "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
        "private_bytes": int(counters.PrivateUsage),
        "peak_pagefile_bytes": int(counters.PeakPagefileUsage),
    }


def _mac_memory_status() -> dict[str, int] | None:
    if platform.system() != "Darwin":
        return None
    try:
        total = int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError):
        try:
            total = int(subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True, stderr=subprocess.DEVNULL, timeout=1
            ).strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        vm_stats = subprocess.check_output(["vm_stat"], text=True, timeout=1)
        page_counts = {}
        for line in vm_stats.splitlines():
            if ":" not in line:
                continue
            name, count = line.split(":", 1)
            try:
                page_counts[name.strip()] = int(count.strip().rstrip("."))
            except ValueError:
                continue
        available_pages = sum(page_counts.get(name, 0) for name in (
            "Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"
        ))
        available = available_pages * page_size
        return {"total_physical_bytes": total, "available_physical_bytes": available,
                "memory_load_percent": round((total - available) * 100 / total)}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return {"total_physical_bytes": total}


def _system_memory_status() -> dict[str, int] | None:
    return _windows_memory_status() or _mac_memory_status()


def _process_memory() -> dict[str, int] | None:
    windows = _windows_process_memory()
    if windows is not None:
        return windows
    if platform.system() == "Darwin":
        import resource

        # macOS reports ru_maxrss in bytes (Linux reports it in KiB).
        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return {"peak_working_set_bytes": peak}
    return None


@lru_cache(maxsize=1)
def host_info() -> dict[str, Any]:
    cpu_name = platform.processor() or platform.machine()
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                cpu_name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    return {
        "os": platform.platform(),
        "processor": cpu_name,
        "logical_cpu_count": os.cpu_count(),
        "total_physical_memory_bytes": (_system_memory_status() or {}).get("total_physical_bytes"),
    }


def system_memory_info() -> dict[str, int] | None:
    """Return a fresh system-wide memory snapshot (not cached hardware metadata)."""
    return _system_memory_status()


def process_metrics() -> dict[str, Any]:
    result: dict[str, Any] = {
        "cpu_time_seconds": round(time.process_time(), 6),
        "pid": os.getpid(),
    }
    memory = _process_memory()
    if memory is not None:
        result.update(memory)
    return result
