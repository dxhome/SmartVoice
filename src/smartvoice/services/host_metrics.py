"""Compatibility exports for platform resource metrics."""

from smartvoice.adapters.platform.host_metrics import (
    host_info,
    process_metrics,
    system_memory_info,
)

__all__ = ["host_info", "process_metrics", "system_memory_info"]
