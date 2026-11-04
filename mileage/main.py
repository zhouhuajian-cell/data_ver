# main.py
"""GPS状态解析、2M/8M完整性判定与实际有效里程计算。

口径（详见 README）：
- 逐相邻点段判定：时间倒退、跳点、GPS状态不稳定、NMEA校验和错误都会被标记异常。
- 段计入 2M/8M 数据里程：该段未被判为时间/跳点异常，且两个端点都属于同一类别。
- 段计入 GPS覆盖/实际有效里程：在 2M/8M 基础上，要求两端 GPS 状态稳定且校验和正确。
- 文件级：2M 和 8M 缺任一类，文件（及整批数据）标记异常。
"""

import argparse
import os
from dataclasses import dataclass, field
from typing import Optional

from config import (
    BASELINE_TOLERANCE_M,
    INPUT_DIR,
    MAX_REASONABLE_SPEED_MPS,
    MIN_ABNORMAL_DIST_THRESHOLD,
    MIN_EFFECTIVE_MILEAGE_RATIO,
    OUTPUT_DIR,
    ROLE_2M,
    ROLE_8M,
    ROLE_OTHER,
    TIME_GAP_THRESHOLD,
)
from distance_calc import vincenty_2d
from trajectory_parser import TrajectoryPoint, load_trajectory
from utils import export_analysis, export_summary


@dataclass
class SegmentResult:
    trip_no: int
    start_line: int
    end_line: int
    start_timestamp: float
    end_timestamp: float
    time_diff_s: float
    start_status: str
    end_status: str
    start_baseline_m: Optional[float]
    end_baseline_m: Optional[float]
    data_class: str
    distance_m: float
    counted_gps_stable: bool
    counted_2m: bool
    counted_8m: bool
    abnormal: bool
    abnormal_reason: str


@dataclass
class AnalysisResult:
    file_name: str
    file_type: str
    point_count: int
    file_format: str = "未知"
    file_role: str = ROLE_OTHER
    trip_count: int = 0

    # 点数
    gps_stable_point_count: int = 0
    point_2m_count: int = 0
    point_8m_count: int = 0
    point_without_status_count: int = 0
    point_with_status_count: int = 0

    # 相邻段数
    valid_segment_count: int = 0
    valid_2m_segment_count: int = 0
    valid_8m_segment_count: int = 0
    invalid_2m_segment_count: int = 0
    invalid_8m_segment_count: int = 0
    abnormal_segment_count: int = 0
    jump_segment_count: int = 0
    time_issue_segment_count: int = 0
    gps_unstable_segment_count: int = 0
    checksum_error_point_count: int = 0

    # 里程（米）
    raw_distance_m: float = 0.0
    valid_2m_distance_m: float = 0.0
    valid_8m_distance_m: float = 0.0
    gps_stable_distance_m: float = 0.0
    gps_stable_2m_distance_m: float = 0.0
    gps_stable_8m_distance_m: float = 0.0
    effective_distance_m: float = 0.0

    status_counts: dict = field(default_factory=dict)
    file_abnormal_reasons: list = field(default_factory=list)
    parse_issues: list = field(default_factory=list)
    segments: list = field(default_factory=list)

    @property
    def effective_mileage_ratio(self) -> float:
        if self.raw_distance_m <= 0:
            return 0.0
        return self.effective_distance_m / self.raw_distance_m

    @property
    def has_status_field(self) -> bool:
        return self.point_with_status_count > 0

    @property
    def data_2m_complete(self) -> bool:
        return self.valid_2m_segment_count >= 1

    @property
    def data_8m_complete(self) -> bool:
        return self.valid_8m_segment_count >= 1

    @property
    def is_abnormal(self) -> bool:
        return bool(self.file_abnormal_reasons)


def _format_status(point: TrajectoryPoint) -> str:
    return f"{point.solution_status_text}" if point.has_status_field else "无状态字段"


def _segment_time_reasons(time_diff: float) -> list[str]:
    """返回相邻段的时间问题原因（无问题返回空列表）。"""
    if time_diff <= 0:
        return ["时间戳重复或倒退"]
    if time_diff > TIME_GAP_THRESHOLD:
        return [f"时间间隔超过{TIME_GAP_THRESHOLD}秒，已切分新行程"]
    return []


def _analyze_points(analysis: AnalysisResult, load, baseline_tolerance_m: float) -> None:
    points = load.points
    analysis.status_counts = dict(load.status_counts)

    # 文件名 → 角色（2M / 8M / 其他）
    name_lower = os.path.basename(analysis.file_name).lower()
    if "8m" in name_lower:
        analysis.file_role = ROLE_8M
    elif "2m" in name_lower:
        analysis.file_role = ROLE_2M

    analysis.gps_stable_point_count = sum(point.gps_stable for point in points)
    analysis.point_2m_count = sum(
        point.baseline_class(baseline_tolerance_m) == "2M" for point in points
    )
    analysis.point_8m_count = sum(
        point.baseline_class(baseline_tolerance_m) == "8M" for point in points
    )
    analysis.point_without_status_count = sum(
        not point.has_status_field for point in points
    )
    analysis.point_with_status_count = sum(
        point.has_status_field for point in points
    )
    analysis.checksum_error_point_count = sum(
        point.checksum_valid is False for point in points
    )

    if len(points) < 2:
        analysis.file_abnormal_reasons.append("有效轨迹点不足2个")
        return

    trip_no = 1
    for p1, p2 in zip(points, points[1:]):
        time_diff = p2.timestamp - p1.timestamp
        reasons = []

        if time_diff > 0:
            distance_m = vincenty_2d(p1.lon, p1.lat, p2.lon, p2.lat)
            analysis.raw_distance_m += distance_m
        else:
            distance_m = 0.0

        class1 = p1.baseline_class(baseline_tolerance_m)
        class2 = p2.baseline_class(baseline_tolerance_m)
        category_boundary = (
            class1 in {"2M", "8M"} and class2 in {"2M", "8M"} and class1 != class2
        )
        if category_boundary:
            data_class = "2M/8M边界"
            reasons.append("2M/8M切换边界，不计入任何里程")
        elif class1 == class2 and class1 is not None:
            data_class = class1
        elif class1 is None and class2 is None:
            # 纯 GGA/CSV 轨迹本身没有 baseline，不是轨迹质量问题：
            # 只表示该段不参与 2M/8M 里程，不记为逐段异常。
            data_class = "无双M基线"
        else:
            data_class = "类别不一致"
            reasons.append("相邻点2M/8M类别不一致")

        time_reasons = _segment_time_reasons(time_diff)
        if time_diff > TIME_GAP_THRESHOLD:
            trip_no += 1

        counted_gps_stable = False
        counted_2m = False
        counted_8m = False
        in_category = data_class in {"2M", "8M"}

        # 几何有效性：时间正常、非跳点
        geometry_ok = False
        if time_reasons:
            reasons.extend(time_reasons)
            analysis.time_issue_segment_count += 1
            if in_category:
                if data_class == "2M":
                    analysis.invalid_2m_segment_count += 1
                else:
                    analysis.invalid_8m_segment_count += 1
        else:
            allowed_distance = max(
                MIN_ABNORMAL_DIST_THRESHOLD, MAX_REASONABLE_SPEED_MPS * time_diff
            )
            if distance_m > allowed_distance:
                reasons.append(
                    f"轨迹跳点({distance_m:.3f}m > 允许{allowed_distance:.3f}m)"
                )
                analysis.jump_segment_count += 1
                if in_category:
                    if data_class == "2M":
                        analysis.invalid_2m_segment_count += 1
                    else:
                        analysis.invalid_8m_segment_count += 1
            else:
                geometry_ok = True

        # GPS 覆盖里程与 2M/8M 无关：只要两端状态稳定、校验和正确、
        # 且该段不是时间异常/跳点，就计入。这样纯 GGA/CSV 轨迹（没有 baseline）
        # 也能算出 GPS 覆盖里程。
        if geometry_ok and p1.gps_stable and p2.gps_stable:
            counted_gps_stable = True
            analysis.gps_stable_distance_m += distance_m

        if geometry_ok and in_category:
            analysis.valid_segment_count += 1
            if data_class == "2M":
                counted_2m = True
                analysis.valid_2m_segment_count += 1
                analysis.valid_2m_distance_m += distance_m
            else:
                counted_8m = True
                analysis.valid_8m_segment_count += 1
                analysis.valid_8m_distance_m += distance_m

            if counted_gps_stable:
                if data_class == "2M":
                    analysis.gps_stable_2m_distance_m += distance_m
                else:
                    analysis.gps_stable_8m_distance_m += distance_m
            else:
                analysis.gps_unstable_segment_count += 1
                bad_checksum = not (p1.checksum_ok and p2.checksum_ok)
                if bad_checksum:
                    reasons.append(
                        "起点或终点NMEA校验和错误，"
                        f"{data_class}数据里程仍计入，但不计入GPS覆盖/实际有效里程"
                    )
                if not p1.has_status_field or not p2.has_status_field:
                    reasons.append("缺少GPS状态，不计入GPS覆盖/实际有效里程")
                elif not bad_checksum:
                    reasons.append("GPS状态不稳定，不计入GPS覆盖/实际有效里程")

        abnormal = bool(reasons)
        if abnormal:
            analysis.abnormal_segment_count += 1
        analysis.segments.append(
            SegmentResult(
                trip_no=trip_no,
                start_line=p1.line_no,
                end_line=p2.line_no,
                start_timestamp=p1.timestamp,
                end_timestamp=p2.timestamp,
                time_diff_s=time_diff,
                start_status=_format_status(p1),
                end_status=_format_status(p2),
                start_baseline_m=p1.baseline_m,
                end_baseline_m=p2.baseline_m,
                data_class=data_class,
                distance_m=distance_m,
                counted_gps_stable=counted_gps_stable,
                counted_2m=counted_2m,
                counted_8m=counted_8m,
                abnormal=abnormal,
                abnormal_reason="；".join(reasons),
            )
        )

    analysis.trip_count = trip_no
    analysis.effective_distance_m = analysis.gps_stable_2m_distance_m + analysis.gps_stable_8m_distance_m


def _file_level_reasons(analysis: AnalysisResult) -> None:
    """文件级异常判定：GPS状态、2M/8M完整性、里程健康度。"""
    if analysis.point_count < 2:
        analysis.file_abnormal_reasons.append("缺少完整2M数据（至少需要1个有效相邻段）")
        analysis.file_abnormal_reasons.append("缺少完整8M数据（至少需要1个有效相邻段）")
        return

    if not analysis.has_status_field:
        analysis.file_abnormal_reasons.append(
            "缺少GPS状态字段，无法计算GPS覆盖里程/实际有效里程"
        )
    elif analysis.gps_stable_point_count == 0:
        analysis.file_abnormal_reasons.append("存在GPS状态字段，但没有任何稳定状态点")

    # 若本文件只含单一类别、且文件名标识了另一种类别，说明同批数据里缺了另一半
    only_class = None
    if analysis.point_2m_count and not analysis.point_8m_count:
        only_class = ROLE_2M
    elif analysis.point_8m_count and not analysis.point_2m_count:
        only_class = ROLE_8M
    missing_role = None
    if only_class == ROLE_2M and analysis.file_role == ROLE_8M:
        missing_role = ROLE_2M
    elif only_class == ROLE_8M and analysis.file_role == ROLE_2M:
        missing_role = ROLE_8M

    def _missing_message(role: str) -> str:
        if missing_role == role:
            return (
                f"缺少{role}数据：本文件为{analysis.file_role}文件，"
                f"整批数据中必须同时提供2M和8M"
            )
        return f"缺少完整{role}数据（至少需要1个有效相邻段）"

    if not analysis.data_2m_complete:
        detail = ""
        if analysis.point_2m_count:
            detail = (
                f"（有{analysis.point_2m_count}个2M点，"
                f"{analysis.invalid_2m_segment_count}个2M段被判异常）"
            )
        analysis.file_abnormal_reasons.append(_missing_message(ROLE_2M) + detail)
    if not analysis.data_8m_complete:
        detail = ""
        if analysis.point_8m_count:
            detail = (
                f"（有{analysis.point_8m_count}个8M点，"
                f"{analysis.invalid_8m_segment_count}个8M段被判异常）"
            )
        analysis.file_abnormal_reasons.append(_missing_message(ROLE_8M) + detail)

    if analysis.checksum_error_point_count:
        analysis.file_abnormal_reasons.append(
            f"NMEA校验和错误{analysis.checksum_error_point_count}个点"
        )
    if analysis.time_issue_segment_count:
        analysis.file_abnormal_reasons.append(
            f"时间戳重复/倒退/超时{analysis.time_issue_segment_count}个相邻段"
        )
    if analysis.jump_segment_count:
        analysis.file_abnormal_reasons.append(
            f"轨迹跳点{analysis.jump_segment_count}个相邻段"
        )

    if analysis.raw_distance_m > 0:
        if analysis.effective_distance_m <= 0:
            analysis.file_abnormal_reasons.append(
                "实际有效里程为0（没有GPS覆盖且属于2M/8M的有效段）"
            )
        elif analysis.effective_mileage_ratio < MIN_EFFECTIVE_MILEAGE_RATIO:
            analysis.file_abnormal_reasons.append(
                f"实际有效里程占比过低({analysis.effective_mileage_ratio:.1%} < "
                f"{MIN_EFFECTIVE_MILEAGE_RATIO:.0%})"
            )


def analyze_trajectory(
    file_name: str,
    *,
    baseline_tolerance_m: float = BASELINE_TOLERANCE_M,
    verbose: bool = True,
) -> AnalysisResult:
    """解析轨迹、标记异常，并计算 GPS覆盖 / 2M / 8M / 实际有效里程。"""
    load = load_trajectory(file_name, include_diagnostics=True, verbose=verbose)
    analysis = AnalysisResult(
        file_name=load.path or file_name,
        file_type=load.file_type,
        file_format=load.format,
        point_count=len(load.points),
        parse_issues=list(load.issues),
    )
    _analyze_points(analysis, load, baseline_tolerance_m)
    _file_level_reasons(analysis)
    return analysis


def describe_role(analysis: AnalysisResult) -> str:
    if analysis.file_role != ROLE_OTHER:
        return analysis.file_role
    return "未命名2M/8M"


def _print_analysis(analysis: AnalysisResult, output_path: Optional[str] = None) -> None:
    print("\n" + "=" * 72)
    print(f"文件：{os.path.basename(analysis.file_name)}（类型：{analysis.file_format}）")
    print(f"文件判定：{'异常' if analysis.is_abnormal else '正常'}")
    if analysis.file_abnormal_reasons:
        for reason in analysis.file_abnormal_reasons:
            print(f"  - {reason}")
    print(f"轨迹点：{analysis.point_count}，行程：{analysis.trip_count}")
    print(
        f"GPS覆盖点：{analysis.gps_stable_point_count}，"
        f"2M点/有效段：{analysis.point_2m_count}/{analysis.valid_2m_segment_count}，"
        f"8M点/有效段：{analysis.point_8m_count}/{analysis.valid_8m_segment_count}"
    )
    print(
        f"2M/8M完整性：2M={'完整' if analysis.data_2m_complete else '缺失'}，"
        f"8M={'完整' if analysis.data_8m_complete else '缺失'}"
    )
    print(f"GPS覆盖里程：{analysis.gps_stable_distance_m:.3f} m")
    print(f"2M里程：{analysis.valid_2m_distance_m:.3f} m（GPS覆盖部分 {analysis.gps_stable_2m_distance_m:.3f} m）")
    print(f"8M里程：{analysis.valid_8m_distance_m:.3f} m（GPS覆盖部分 {analysis.gps_stable_8m_distance_m:.3f} m）")
    print(f"实际有效里程：{analysis.effective_distance_m:.3f} m "
          f"({analysis.effective_distance_m / 1000:.4f} km)")
    print(
        f"异常相邻段：{analysis.abnormal_segment_count}，"
        f"解析/校验异常行：{len(analysis.parse_issues)}"
    )
    if output_path:
        print(f"结果文件：{output_path}")
    print("=" * 72)


def calculate_trajectory_distance(
    file_name: str,
    use_high_precision: bool = True,
    baseline_tolerance_m: float = BASELINE_TOLERANCE_M,
    *,
    verbose: bool = True,
    output_dir: Optional[str] = None,
) -> AnalysisResult:
    # use_high_precision 保留用于兼容旧调用；当前统一采用 Vincenty。
    _ = use_high_precision
    analysis = analyze_trajectory(
        file_name,
        baseline_tolerance_m=baseline_tolerance_m,
        verbose=verbose,
    )
    output_path = export_analysis(analysis, output_dir=output_dir)
    if verbose:
        _print_analysis(analysis, output_path)
    return analysis


# ---------------------------------------------------------------------------
# 批量：一个 input 目录 = 一批数据，2M 与 8M 缺一个即整批异常
# ---------------------------------------------------------------------------


@dataclass
class BatchSummary:
    results: list = field(default_factory=list)
    effective_2m_complete: bool = False
    effective_8m_complete: bool = False
    abnormal_reasons: list = field(default_factory=list)
    summary_csv_path: Optional[str] = None

    # 里程（米，整批）
    effective_distance_m: float = 0.0
    gps_stable_distance_m: float = 0.0
    distance_2m_m: float = 0.0
    distance_8m_m: float = 0.0

    @property
    def is_abnormal(self) -> bool:
        return bool(self.abnormal_reasons)

    @property
    def abnormal_file_count(self) -> int:
        return sum(result.is_abnormal for result in self.results)


def list_trajectory_files(input_dir: str) -> list[str]:
    suffixes = {".txt", ".csv"}
    files = []
    for name in sorted(os.listdir(input_dir)):
        path = os.path.join(input_dir, name)
        if os.path.isfile(path) and os.path.splitext(name)[1].lower() in suffixes:
            files.append(path)
    return files


def analyze_directory(
    input_dir: str = INPUT_DIR,
    output_dir: str = OUTPUT_DIR,
    *,
    baseline_tolerance_m: float = BASELINE_TOLERANCE_M,
    verbose: bool = True,
) -> BatchSummary:
    """分析目录内全部轨迹文件，并给出整批数据的 2M/8M 完整性判定。"""
    files = list_trajectory_files(input_dir)
    if not files:
        raise FileNotFoundError(f"{input_dir} 下没有 .txt/.csv 轨迹文件")

    summary = BatchSummary()
    for path in files:
        result = analyze_trajectory(
            path,
            baseline_tolerance_m=baseline_tolerance_m,
            verbose=verbose,
        )
        export_analysis(result, output_dir=output_dir)
        summary.results.append(result)

    # 整批有效性：只要存在有效 2M/8M 里程，就认为该类别数据存在
    summary.distance_2m_m = sum(result.valid_2m_distance_m for result in summary.results)
    summary.distance_8m_m = sum(result.valid_8m_distance_m for result in summary.results)
    summary.gps_stable_distance_m = sum(
        result.gps_stable_distance_m for result in summary.results
    )
    summary.effective_distance_m = sum(
        result.effective_distance_m for result in summary.results
    )
    summary.effective_2m_complete = summary.distance_2m_m > 0
    summary.effective_8m_complete = summary.distance_8m_m > 0

    if not summary.effective_2m_complete:
        summary.abnormal_reasons.append("整批数据缺少有效的2M里程（2M数据缺失或全部异常）")
    if not summary.effective_8m_complete:
        summary.abnormal_reasons.append("整批数据缺少有效的8M里程（8M数据缺失或全部异常）")
    if summary.effective_distance_m <= 0:
        summary.abnormal_reasons.append("整批数据的实际有效里程为0")
    failed = [r for r in summary.results if r.is_abnormal]
    if failed:
        summary.abnormal_reasons.append(
            f"{len(failed)}/{len(summary.results)}个文件存在异常：" +
            "、".join(os.path.basename(r.file_name) for r in failed)
        )

    summary.summary_csv_path = export_summary(summary, output_dir=output_dir)
    if verbose:
        _print_summary(summary)
    return summary


def _print_summary(summary: BatchSummary) -> None:
    print("\n" + "#" * 72)
    print("整批数据判定：" + ("异常" if summary.is_abnormal else "正常"))
    for reason in summary.abnormal_reasons:
        print(f"  - {reason}")
    print(
        f"2M数据：{'完整' if summary.effective_2m_complete else '缺失'}，"
        f"8M数据：{'完整' if summary.effective_8m_complete else '缺失'}"
    )
    print(f"文件数：{len(summary.results)}（异常 {summary.abnormal_file_count}）")
    print(f"GPS覆盖里程：{summary.gps_stable_distance_m:.3f} m")
    print(f"2M里程：{summary.distance_2m_m:.3f} m")
    print(f"8M里程：{summary.distance_8m_m:.3f} m")
    print(f"实际有效里程：{summary.effective_distance_m:.3f} m "
          f"({summary.effective_distance_m / 1000:.4f} km)")
    print(f"汇总文件：{summary.summary_csv_path}")
    print("#" * 72)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="WGS84轨迹、GPS状态及2M/8M有效里程计算")
    parser.add_argument("--file", help="input目录下的单个轨迹文件名（可选）")
    parser.add_argument("--input", help="轨迹目录，默认 config.INPUT_DIR")
    parser.add_argument("--output", help="输出目录，默认 config.OUTPUT_DIR")
    parser.add_argument(
        "--baseline-tolerance",
        type=float,
        default=BASELINE_TOLERANCE_M,
        help=f"2M/8M基线分类容差（米），默认{BASELINE_TOLERANCE_M}",
    )
    # 保留旧参数，避免原来的执行命令报错。
    parser.add_argument("--fast", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    input_dir = args.input or INPUT_DIR
    output_dir = args.output or OUTPUT_DIR

    if args.file:
        file_path = (
            args.file if os.path.isabs(args.file) else os.path.join(input_dir, args.file)
        )
        calculate_trajectory_distance(
            file_path,
            use_high_precision=not args.fast,
            baseline_tolerance_m=args.baseline_tolerance,
        )
        return

    analyze_directory(
        input_dir=input_dir,
        output_dir=output_dir,
        baseline_tolerance_m=args.baseline_tolerance,
    )


if __name__ == "__main__":
    main()
