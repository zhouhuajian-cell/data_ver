"""Tests for GPS-status parsing, anomaly marking and effective mileage.

Run with:  python -m unittest test_analysis -v
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from main import AnalysisResult, calculate_trajectory_distance
from trajectory_parser import parse_line

# Vincenty distance for one 1e-5 degree step in longitude at latitude 31.2,
# used only to sanity check magnitude; assertions use counts, not exact metres.
LEG_M = 0.952189

LONS = [121.40000, 121.40001, 121.40002, 121.40003]
WEEK = 2400


def gpfpd(
    second: float,
    lon: float,
    *,
    baseline: float,
    status: int,
    lat: float = 31.2,
    checksum: bool = True,
) -> str:
    payload = (
        f"GPFPD,{WEEK},{second:.2f},0,0,0,{lat:.8f},{lon:.8f},10.0,"
        f"0,0,0,{baseline:.3f},20,20,{status}"
    )
    value = 0
    for char in payload:
        value ^= ord(char)
    text = f"${payload}*{value:02X}"
    if not checksum:
        return f"${payload}*00\n"
    return f"{text}\n"


def nmea_checksum(payload: str) -> str:
    value = 0
    for char in payload:
        value ^= ord(char)
    return f"{value:02X}"


def _nmea_coord(value: float) -> str:
    """Decimal degrees -> NMEA ddmm.mmmm / dddmm.mmmm format."""
    degrees = int(abs(value))
    minutes = (abs(value) - degrees) * 60
    return f"{degrees:02d}{minutes:.8f}"


def gpgga(second: float, lon: float, lat: float = 31.2) -> str:
    payload = (
        f"GPGGA,{int(second // 3600):02d}{(int(second) // 60) % 60:02d}{second % 60:05.2f},"
        f"{_nmea_coord(lat)},N,{_nmea_coord(lon)},E,1,12,1.0,10.0,M,0.0,M,"
    )
    return f"${payload}*{nmea_checksum(payload)}\n"


def csv_row(timestamp: float, lon: float, lat: float = 31.2) -> str:
    return f"{timestamp},{lat:.8f},{lon:.8f},10.0\n"


def write(tmp_dir: str, name: str, lines: list[str]) -> str:
    path = os.path.join(tmp_dir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    return path


class RunningInput:
    """Context manager that points the parser at a temporary input dir."""

    def __init__(self, lines: list[str], name: str = "sample.txt"):
        self.lines = lines
        self.name = name

    def __enter__(self):
        self._temp = tempfile.TemporaryDirectory()
        self.dir = self._temp.name
        write(self.dir, self.name, self.lines)
        self._patch = patch("trajectory_parser.INPUT_DIR", self.dir)
        self._patch.start()
        return self.name

    def __exit__(self, *exc):
        self._patch.stop()
        self._temp.cleanup()
        return False


class ParserTests(unittest.TestCase):
    def test_gpfpd_status_baseline_and_checksum(self):
        point = parse_line(
            gpfpd(100.0, 121.4, baseline=2.05, status=11).strip(), line_no=7
        )
        self.assertIsNotNone(point)
        self.assertEqual(point.line_no, 7)
        self.assertEqual(point.solution_status, 11)
        self.assertTrue(point.gps_stable)
        self.assertEqual(point.baseline_class(), "2M")
        self.assertTrue(point.checksum_valid)

    def test_unstable_status_codes(self):
        for status in (0, 3):
            point = parse_line(
                gpfpd(100.0, 121.4, baseline=2.0, status=status).strip(), line_no=1
            )
            self.assertIsNotNone(point)
            self.assertFalse(point.gps_stable, f"status {status} must be unstable")

    def test_bad_checksum_makes_point_unstable(self):
        point = parse_line(
            gpfpd(100.0, 121.4, baseline=2.0, status=1, checksum=False).strip(),
            line_no=1,
        )
        self.assertIsNotNone(point)
        self.assertFalse(point.gps_stable)
        self.assertIs(point.checksum_valid, False)

    def test_preserves_point_when_status_or_baseline_is_bad(self):
        payload = "GPFPD,2400,100.00,0,0,0,31.2,121.4,10,0,0,0,,20,20,X"
        point = parse_line(f"${payload}*{nmea_checksum(payload)}", line_no=3)

        self.assertIsNotNone(point)
        self.assertIsNone(point.baseline_m)
        self.assertIsNone(point.solution_status)
        self.assertIn("基线长度无法解析", point.parse_warnings)
        self.assertIn("GPS状态无法解析", point.parse_warnings)

    def test_gpgga_gives_position_and_stability(self):
        point = parse_line(gpgga(100.0, 121.4).strip(), line_no=2)
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point.lon, 121.4, places=7)
        self.assertAlmostEqual(point.lat, 31.2, places=7)
        self.assertTrue(point.gps_stable)
        self.assertIsNone(point.baseline_class())

    def test_csv_without_status_is_reported_as_missing(self):
        point = parse_line(csv_row(100.0, 121.4).strip(), line_no=1)
        self.assertIsNotNone(point)
        self.assertIsNone(point.solution_status)
        self.assertFalse(point.gps_stable)

    def test_csv_with_status_and_baseline_columns(self):
        point = parse_line("100.0,31.2,121.4,10.0,1,2.0", line_no=1)
        self.assertIsNotNone(point)
        self.assertEqual(point.solution_status, 1)
        self.assertTrue(point.gps_stable)
        self.assertEqual(point.baseline_class(), "2M")


class MileageTests(unittest.TestCase):
    def test_stable_2m_and_8m_mileage(self):
        lines = [
            gpfpd(100.00, 121.40000, baseline=2.0, status=1),
            gpfpd(101.00, 121.40001, baseline=2.1, status=2),
            gpfpd(102.00, 121.40002, baseline=8.0, status=3),
            gpfpd(103.00, 121.40003, baseline=8.1, status=12),
            gpfpd(104.00, 121.40004, baseline=8.0, status=12),
        ]
        with RunningInput(lines) as name:
            result = calculate_trajectory_distance(name, verbose=False)

        self.assertTrue(result.data_2m_complete)
        self.assertTrue(result.data_8m_complete)
        self.assertAlmostEqual(result.valid_2m_distance_m, LEG_M, places=4)
        self.assertAlmostEqual(result.valid_8m_distance_m, 2 * LEG_M, places=4)
        # 2M stable leg, then the 2M->8M boundary (excluded), then 8M status 3
        # (unstable), then 8M stable.
        self.assertAlmostEqual(result.gps_stable_distance_m, 2 * LEG_M, places=4)
        self.assertAlmostEqual(result.gps_stable_2m_distance_m, LEG_M, places=4)
        self.assertAlmostEqual(result.gps_stable_8m_distance_m, LEG_M, places=4)
        self.assertAlmostEqual(result.effective_distance_m, 2 * LEG_M, places=4)
        self.assertTrue(
            any("GPS状态不稳定" in row.abnormal_reason for row in result.segments)
        )

    def test_missing_8m_marks_file_abnormal(self):
        lines = [
            gpfpd(100.00, 121.40000, baseline=2.0, status=1),
            gpfpd(101.00, 121.40001, baseline=2.0, status=1),
        ]
        with RunningInput(lines, name="only_2m.txt") as name:
            result = calculate_trajectory_distance(name, verbose=False)

        self.assertFalse(result.data_8m_complete)
        self.assertTrue(result.data_2m_complete)
        self.assertTrue(result.is_abnormal)
        self.assertTrue(any("8M" in reason for reason in result.file_abnormal_reasons))
        # Missing 8M must not destroy the mileage that is real.
        self.assertAlmostEqual(result.effective_distance_m, LEG_M, places=4)

    def test_missing_2m_marks_file_abnormal(self):
        lines = [
            gpfpd(100.00, 121.40000, baseline=8.0, status=1),
            gpfpd(101.00, 121.40001, baseline=8.0, status=1),
        ]
        with RunningInput(lines, name="only_8m.txt") as name:
            result = calculate_trajectory_distance(name, verbose=False)

        self.assertFalse(result.data_2m_complete)
        self.assertTrue(result.data_8m_complete)
        self.assertTrue(result.is_abnormal)
        self.assertTrue(any("2M" in reason for reason in result.file_abnormal_reasons))

    def test_checksum_error_is_not_counted_as_stable_mileage(self):
        good = gpfpd(100.00, 121.40000, baseline=2.0, status=1)
        bad = gpfpd(101.00, 121.40001, baseline=2.0, status=1, checksum=False)
        with RunningInput([good, bad], name="bad_checksum.txt") as name:
            result = calculate_trajectory_distance(name, verbose=False)

        # 坐标可能已损坏：几何距离仍计入 2M 数据里程，
        # 但 GPS 状态不可信，不计入 GPS 覆盖里程和实际有效里程。
        self.assertAlmostEqual(result.valid_2m_distance_m, LEG_M, places=4)
        self.assertAlmostEqual(result.gps_stable_distance_m, 0.0, places=6)
        self.assertAlmostEqual(result.effective_distance_m, 0.0, places=6)
        self.assertEqual(result.valid_2m_segment_count, 1)
        self.assertEqual(result.checksum_error_point_count, 1)
        self.assertEqual(len(result.parse_issues), 1)
        self.assertIn("校验和", result.parse_issues[0].reason)
        self.assertTrue(any("校验和" in r for r in result.file_abnormal_reasons))

    def test_duplicate_timestamp_is_anomaly_and_not_counted(self):
        lines = [
            gpfpd(100.00, 121.40000, baseline=2.0, status=1),
            gpfpd(100.00, 121.40001, baseline=2.0, status=1),
            gpfpd(101.00, 121.40002, baseline=2.0, status=1),
            gpfpd(102.00, 121.40003, baseline=8.0, status=1),
        ]
        with RunningInput(lines, name="dup.txt") as name:
            result = calculate_trajectory_distance(name, verbose=False)

        self.assertTrue(result.is_abnormal)
        self.assertTrue(any("时间" in r for r in result.file_abnormal_reasons))
        self.assertAlmostEqual(result.effective_distance_m, LEG_M, places=4)

    def test_jump_point_is_anomaly_and_not_counted(self):
        lines = [
            gpfpd(100.00, 121.40000, baseline=2.0, status=1),
            gpfpd(101.00, 121.90000, baseline=2.0, status=1),  # ~48 km in 1 s
            gpfpd(102.00, 121.40002, baseline=8.0, status=1),
        ]
        with RunningInput(lines, name="jump.txt") as name:
            result = calculate_trajectory_distance(name, verbose=False)

        self.assertTrue(result.is_abnormal)
        self.assertEqual(result.jump_segment_count, 2)
        self.assertAlmostEqual(result.valid_2m_distance_m, 0.0, places=6)
        self.assertAlmostEqual(result.valid_8m_distance_m, 0.0, places=6)
        self.assertAlmostEqual(result.effective_distance_m, 0.0, places=6)


class BatchSummaryTests(unittest.TestCase):
    def test_summary_marks_missing_8m_across_input(self):
        two_m = [
            gpfpd(100.00, 121.40000, baseline=2.0, status=1),
            gpfpd(110.00, 121.44000, baseline=2.0, status=1),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_dir = os.path.join(temp_dir, "input")
            output_dir = os.path.join(temp_dir, "output")
            os.makedirs(input_dir)
            write(input_dir, "batch_2m.txt", two_m)
            from main import analyze_directory

            summaries = analyze_directory(
                input_dir=input_dir, output_dir=output_dir, verbose=False
            )

            self.assertTrue(os.path.exists(summaries.summary_csv_path))
            self.assertFalse(summaries.effective_2m_complete)
            self.assertFalse(summaries.effective_8m_complete)
            self.assertTrue(summaries.is_abnormal)
            self.assertTrue(any("8M" in reason for reason in summaries.abnormal_reasons))

            with open(summaries.summary_csv_path, "r", encoding="utf-8-sig") as handle:
                exported = handle.read()
            self.assertIn("实际有效里程", exported)
            self.assertIn("2M", exported)
            self.assertIn("8M", exported)

    def test_csv_batch_without_status_is_abnormal_and_warns(self):
        rows = [csv_row(100.0 + i, 121.4 + i * 1e-4) for i in range(5)]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_dir = os.path.join(temp_dir, "input")
            output_dir = os.path.join(temp_dir, "output")
            os.makedirs(input_dir)
            write(input_dir, "plain.csv", rows)
            from main import analyze_directory

            summary = analyze_directory(
                input_dir=input_dir, output_dir=output_dir, verbose=False
            )

            self.assertEqual(len(summary.results), 1)
            result = summary.results[0]
            self.assertTrue(result.is_abnormal)
            self.assertTrue(
                any("缺少GPS状态" in reason for reason in result.file_abnormal_reasons)
            )
            self.assertTrue(
                any("缺少完整2M数据" in reason for reason in result.file_abnormal_reasons)
            )
            self.assertAlmostEqual(result.effective_distance_m, 0.0, places=6)
            self.assertEqual(result.point_without_status_count, 5)


class ReportTests(unittest.TestCase):
    def test_exports_calculation_csv_with_effective_mileage(self):
        lines = [
            gpfpd(100.00, 121.40000, baseline=2.0, status=1),
            gpfpd(101.00, 121.40001, baseline=2.0, status=1),
            gpfpd(102.00, 121.40002, baseline=8.0, status=1),
            gpfpd(103.00, 121.40003, baseline=8.0, status=1),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_dir = os.path.join(temp_dir, "input")
            output_dir = os.path.join(temp_dir, "output")
            os.makedirs(input_dir)
            write(input_dir, "export_sample.txt", lines)
            with patch("trajectory_parser.INPUT_DIR", input_dir):
                result = calculate_trajectory_distance(
                    "export_sample.txt", verbose=False, output_dir=output_dir
                )

            result_path = os.path.join(output_dir, "analysis_export_sample.txt.csv")
            self.assertFalse(result.is_abnormal)
            self.assertTrue(os.path.exists(result_path))
            with open(result_path, "r", encoding="utf-8-sig") as handle:
                exported = handle.read()
            self.assertIn("GPS覆盖且2M里程(m)", exported)
            self.assertIn("GPS覆盖且8M里程(m)", exported)
            self.assertIn("实际有效里程(m)", exported)


if __name__ == "__main__":
    unittest.main()
