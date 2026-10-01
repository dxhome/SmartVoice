from __future__ import annotations

import unittest
from unittest.mock import patch

from smartvoice.adapters.platform import host_metrics


class HostMetricsTests(unittest.TestCase):
    def test_host_info_reports_platform_cpu_and_memory_shape(self):
        host_metrics.host_info.cache_clear()
        try:
            with patch.object(host_metrics, "_system_memory_status", return_value={"total_physical_bytes": 1234}):
                info = host_metrics.host_info()
        finally:
            host_metrics.host_info.cache_clear()

        self.assertTrue(info["os"])
        self.assertTrue(info["processor"])
        self.assertGreaterEqual(info["logical_cpu_count"], 1)
        self.assertEqual(info["total_physical_memory_bytes"], 1234)

    def test_process_metrics_reports_cpu_time_and_pid(self):
        with patch.object(host_metrics, "_process_memory", return_value={
            "working_set_bytes": 100,
            "peak_working_set_bytes": 120,
            "private_bytes": 80,
            "peak_pagefile_bytes": 140,
        }):
            metrics = host_metrics.process_metrics()

        self.assertGreaterEqual(metrics["cpu_time_seconds"], 0)
        self.assertGreater(metrics["pid"], 0)
        self.assertEqual(metrics["working_set_bytes"], 100)
        self.assertEqual(metrics["peak_working_set_bytes"], 120)
        self.assertEqual(metrics["private_bytes"], 80)
        self.assertEqual(metrics["peak_pagefile_bytes"], 140)

    def test_linux_memory_status_uses_proc_meminfo(self):
        meminfo = "MemTotal: 1000 kB\nMemAvailable: 250 kB\n"
        with (
            patch.object(host_metrics.os, "name", "posix"),
            patch.object(host_metrics.platform, "system", return_value="Linux"),
            patch.object(host_metrics.Path, "read_text", return_value=meminfo),
        ):
            self.assertEqual(host_metrics.system_memory_info(), {
                "total_physical_bytes": 1_024_000,
                "available_physical_bytes": 256_000,
                "memory_load_percent": 75,
            })


if __name__ == "__main__":
    unittest.main()
