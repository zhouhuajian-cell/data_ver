# utils.py
import csv
import os

import config


def _yes_no(value) -> str:
    return "是" if value else "否"


def _m(value: float) -> float:
    return round(value, 4)


def _km(value: float) -> float:
    return round(value / 1000, 6)


def analysis_report_path(analysis, output_dir=None) -> str:
    safe_name = os.path.basename(analysis.file_name)
    base = output_dir or config.OUTPUT_DIR
    return os.path.join(base, f"analysis_{safe_name}.csv")


def export_analysis(analysis, output_dir=None) -> str:
    """导出单文件汇总、GPS状态分布、异常行和相邻点计算明细。"""
    output_dir = output_dir or config.OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)
    out_path = analysis_report_path(analysis, output_dir)

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["===== 文件汇总 ====="])
        writer.writerow(["文件名", os.path.basename(analysis.file_name)])
        writer.writerow(["文件类型", analysis.file_type])
        writer.writerow(["文件角色", analysis.file_role])
        writer.writerow(["文件判定", "异常" if analysis.is_abnormal else "正常"])
        writer.writerow(["文件级异常原因", "；".join(analysis.file_abnormal_reasons)])
        writer.writerow(["轨迹点数", analysis.point_count])
        writer.writerow(["行程数", analysis.trip_count])
        writer.writerow(["GPS覆盖点数", analysis.gps_stable_point_count])
        writer.writerow(["无GPS状态字段点数", analysis.point_without_status_count])
        writer.writerow(["2M点数", analysis.point_2m_count])
        writer.writerow(["8M点数", analysis.point_8m_count])
        writer.writerow(["2M有效相邻段数", analysis.valid_2m_segment_count])
        writer.writerow(["8M有效相邻段数", analysis.valid_8m_segment_count])
        writer.writerow(["2M被剔除相邻段数", analysis.invalid_2m_segment_count])
        writer.writerow(["8M被剔除相邻段数", analysis.invalid_8m_segment_count])
        writer.writerow(["2M数据完整", _yes_no(analysis.data_2m_complete)])
        writer.writerow(["8M数据完整", _yes_no(analysis.data_8m_complete)])
        writer.writerow(["原始轨迹长度(m)", _m(analysis.raw_distance_m)])
        writer.writerow(["GPS覆盖里程(m)", _m(analysis.gps_stable_distance_m)])
        writer.writerow(["GPS覆盖里程(km)", _km(analysis.gps_stable_distance_m)])
        writer.writerow(["2M数据里程(m)", _m(analysis.valid_2m_distance_m)])
        writer.writerow(["2M数据里程(km)", _km(analysis.valid_2m_distance_m)])
        writer.writerow(["8M数据里程(m)", _m(analysis.valid_8m_distance_m)])
        writer.writerow(["8M数据里程(km)", _km(analysis.valid_8m_distance_m)])
        writer.writerow(["GPS覆盖且2M里程(m)", _m(analysis.gps_stable_2m_distance_m)])
        writer.writerow(["GPS覆盖且8M里程(m)", _m(analysis.gps_stable_8m_distance_m)])
        writer.writerow(["实际有效里程(m)", _m(analysis.effective_distance_m)])
        writer.writerow(["实际有效里程(km)", _km(analysis.effective_distance_m)])
        writer.writerow(["实际有效里程占比", f"{analysis.effective_mileage_ratio:.2%}"])
        writer.writerow(["异常相邻段数", analysis.abnormal_segment_count])
        writer.writerow(["跳点相邻段数", analysis.jump_segment_count])
        writer.writerow(["时间异常相邻段数", analysis.time_issue_segment_count])
        writer.writerow(["GPS不稳定相邻段数", analysis.gps_unstable_segment_count])
        writer.writerow(["NMEA校验和错误点数", analysis.checksum_error_point_count])
        writer.writerow(["解析/校验异常行数", len(analysis.parse_issues)])

        writer.writerow([])
        writer.writerow(["===== GPS状态分布 ====="])
        writer.writerow(["状态", "点数"])
        if analysis.status_counts:
            for status, count in sorted(analysis.status_counts.items()):
                writer.writerow([status, count])
        else:
            writer.writerow(["无状态字段", analysis.point_count])

        writer.writerow([])
        writer.writerow(["===== 解析/校验异常行 ====="])
        writer.writerow(["源文件行号", "异常原因", "原始内容"])
        for issue in analysis.parse_issues:
            writer.writerow([issue.line_no, issue.reason, issue.content])

        writer.writerow([])
        writer.writerow(["===== 相邻点里程与异常明细 ====="])
        writer.writerow([
            "行程编号",
            "起点行号",
            "终点行号",
            "起点时间戳",
            "终点时间戳",
            "时间差(s)",
            "起点GPS状态",
            "终点GPS状态",
            "起点基线(m)",
            "终点基线(m)",
            "数据类别",
            "距离(m)",
            "计入GPS覆盖里程",
            "计入2M里程",
            "计入8M里程",
            "是否异常",
            "异常原因",
        ])
        for segment in analysis.segments:
            writer.writerow([
                segment.trip_no,
                segment.start_line,
                segment.end_line,
                segment.start_timestamp,
                segment.end_timestamp,
                round(segment.time_diff_s, 6),
                segment.start_status,
                segment.end_status,
                segment.start_baseline_m,
                segment.end_baseline_m,
                segment.data_class,
                round(segment.distance_m, 4),
                _yes_no(segment.counted_gps_stable),
                _yes_no(segment.counted_2m),
                _yes_no(segment.counted_8m),
                _yes_no(segment.abnormal),
                segment.abnormal_reason,
            ])
    return out_path


def export_summary(summary, output_dir=None) -> str:
    """导出整批数据的 2M/8M 完整性判定与有效里程汇总。"""
    output_dir = output_dir or config.OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "summary_all_files.csv")
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["===== 整批数据判定 ====="])
        writer.writerow(["整批判定", "异常" if summary.is_abnormal else "正常"])
        writer.writerow(["整批异常原因", "；".join(summary.abnormal_reasons)])
        writer.writerow(["2M数据", "完整" if summary.effective_2m_complete else "缺失"])
        writer.writerow(["8M数据", "完整" if summary.effective_8m_complete else "缺失"])
        writer.writerow(["文件数", len(summary.results)])
        writer.writerow(["异常文件数", summary.abnormal_file_count])
        writer.writerow(["GPS覆盖里程(m)", _m(summary.gps_stable_distance_m)])
        writer.writerow(["2M数据里程(m)", _m(summary.distance_2m_m)])
        writer.writerow(["8M数据里程(m)", _m(summary.distance_8m_m)])
        writer.writerow(["实际有效里程(m)", _m(summary.effective_distance_m)])
        writer.writerow(["实际有效里程(km)", _km(summary.effective_distance_m)])

        writer.writerow([])
        writer.writerow(["===== 分文件明细 ====="])
        writer.writerow([
            "文件名",
            "文件角色",
            "判定",
            "异常原因",
            "轨迹点数",
            "GPS覆盖点数",
            "2M点",
            "8M点",
            "2M段",
            "8M段",
            "GPS覆盖里程(m)",
            "2M里程(m)",
            "8M里程(m)",
            "实际有效里程(m)",
            "实际有效里程(km)",
            "有效占比",
        ])
        for result in summary.results:
            writer.writerow([
                os.path.basename(result.file_name),
                result.file_role,
                "异常" if result.is_abnormal else "正常",
                "；".join(result.file_abnormal_reasons),
                result.point_count,
                result.gps_stable_point_count,
                result.point_2m_count,
                result.point_8m_count,
                result.valid_2m_segment_count,
                result.valid_8m_segment_count,
                _m(result.gps_stable_distance_m),
                _m(result.valid_2m_distance_m),
                _m(result.valid_8m_distance_m),
                _m(result.effective_distance_m),
                _km(result.effective_distance_m),
                f"{result.effective_mileage_ratio:.2%}",
            ])
    return out_path


def export_result(file_name: str, seg_data: list, total_2d: float, total_3d: float):
    """导出分段距离与总里程到csv结果文件（保留兼容旧调用）。"""
    out_path = os.path.join(config.OUTPUT_DIR, f"dist_result_{file_name}.csv")
    headers = ["行程段编号", "分段序号", "起点时间戳", "终点时间戳", "地表2D距离(m)", "三维空间距离(m)"]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(seg_data)
        writer.writerow([])
        writer.writerow(["===== 各行程分段汇总 ====="])
        writer.writerow(["全部行程总地表里程(m)", round(total_2d, 4)])
        writer.writerow(["全部行程总地表里程(km)", round(total_2d / 1000, 4)])
        writer.writerow(["全部行程总三维空间里程(m)", round(total_3d, 4)])
        writer.writerow(["全部行程总三维空间里程(km)", round(total_3d / 1000, 4)])
    print(f"\n计算结果已导出至：{out_path}")
