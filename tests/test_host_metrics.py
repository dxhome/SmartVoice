from __future__ import annotations

import unittest
from unittest.mock import patch

from smartvoice.services import host_metrics


class HostMetricsTests(unittest.TestCase):
    def test_host_info_reports_platform_cpu_and_memory_shape(self):
        host_metrics.host_info.cache_clear()
        try:
            with patch.object(host_metrics, "_windows_memory_status", return_value={"total_physical_bytes": 1234}):
                info = host_metrics.host_info()
        finally:
            host_metrics.host_info.cache_clear()

        self.assertTrue(info["os"])
        self.assertTrue(info["processor"])
        self.assertGreaterEqual(info["logical_cpu_count"], 1)
        self.assertEqual(info["total_physical_memory_bytes"], 1234)

    def test_process_metrics_reports_cpu_time_and_pid(self):
        with patch.object(host_metrics, "_windows_process_memory", return_value={
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

    def test_non_windows_memory_unavailability_is_reported_as_none(self):
        with patch.object(host_metrics.os, "name", "posix"):
            self.assertIsNone(host_metrics.system_memory_info())
            self.assertEqual(host_metrics.process_metrics()["pid"], host_metrics.os.getpid())


if __name__ == "__main__":
    unittest.main()
