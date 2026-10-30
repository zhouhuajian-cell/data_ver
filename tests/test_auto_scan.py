"""自动扫描脚本的完整性判断测试（不触发真实参数验证）。"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module():
    spec = importlib.util.spec_from_file_location("auto_verify_scan", ROOT / "tools" / "auto_verify_scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_dataset(base: Path, *, frames: int = 12, lidar_files: int = 4, tag: str = "a") -> Path:
    dataset = base / f"137999_E5-001_yanzhen_20261021_{tag}"
    (dataset / "parameter_yaml" / "20260920").mkdir(parents=True)
    (dataset / "parameter_yaml" / "20260920" / "lidar-1_to_camera-1.yaml").write_text("a: 1\n", encoding="utf-8")
    (dataset / "rtkimu_qj_7683").mkdir(parents=True)
    (dataset / "rtkimu_qj_7683" / "Vehicle(imu1_2_body2).pose").write_text("0 0 0\n", encoding="utf-8")
    cam = dataset / "cam_st_x2f_front"
    cam.mkdir(parents=True)
    for index in range(frames):
        (cam / f"{index:06d}.jpg").write_bytes(b"jpg")
    lidar = dataset / "lidar_hs_pandar128_topmiddle"
    lidar.mkdir(parents=True)
    for index in range(lidar_files):
        (lidar / f"{index:06d}.pcd").write_bytes(b"pcd")
    return dataset


def _age(paths: list[Path], minutes: float) -> None:
    stamp = time.time() - minutes * 60
    for path in paths:
        os.utime(path, (stamp, stamp))


def main() -> int:
    module = _load_module()
    failed = 0

    def check(ok: bool, message: str, detail: str = "") -> None:
        nonlocal failed
        text = message + (f" - {detail}" if detail and not ok else "")
        print(("  [ OK ] " if ok else "  [FAIL] ") + text)
        if not ok:
            failed += 1

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        dataset = _make_dataset(base)
        _age([dataset, dataset / "parameter_yaml", dataset / "rtkimu_qj_7683", dataset / "cam_st_x2f_front"], 120)
        info = module.check_complete(dataset, min_age_min=30, min_cam_frames=10, min_lidar_files=3)
        check(info["ok"] is True, "完整数据集判定为可分析")
        check(info["cameras"]["cam_st_x2f_front"] >= 10, "相机帧数统计正确")

        fresh = _make_dataset(base, tag="b")
        info = module.check_complete(fresh, min_age_min=30, min_cam_frames=10, min_lidar_files=3)
        check(info["ok"] is False and any("写入" in item for item in info["problems"]), "新写入的数据判定为未稳定")

        bad = _make_dataset(base, tag="c")
        (bad / "parameter_yaml" / "20260920" / "lidar-1_to_camera-1.yaml").unlink()
        _age([bad, bad / "parameter_yaml", bad / "rtkimu_qj_7683"], 120)
        info = module.check_complete(bad, min_age_min=30, min_cam_frames=10, min_lidar_files=3)
        check(info["ok"] is False and any("parameter_yaml" in item for item in info["problems"]), "缺标定 yaml 判定为不完整")

        few = _make_dataset(base, frames=3, tag="d")
        _age([few, few / "parameter_yaml", few / "rtkimu_qj_7683", few / "cam_st_x2f_front"], 120)
        info = module.check_complete(few, min_age_min=30, min_cam_frames=10, min_lidar_files=3)
        check(info["ok"] is False and any("相机" in item for item in info["problems"]), "相机帧数不足判定为不完整")

        # ---- 欧洲车型：pose 目录 rtkimu_lvx_125、雷达文件 .las 也要识别 ----
        lynk = _make_dataset(base, tag="e")
        (lynk / "rtkimu_qj_7683").rename(lynk / "rtkimu_lvx_125")
        for path in (lynk / "lidar_hs_pandar128_topmiddle").glob("*.pcd"):
            path.rename(path.with_suffix(".las"))
        _age(
            [lynk, lynk / "parameter_yaml", lynk / "rtkimu_lvx_125", lynk / "cam_st_x2f_front"],
            120,
        )
        info = module.check_complete(lynk, min_age_min=30, min_cam_frames=10, min_lidar_files=3)
        check(info["ok"] is True, "rtkimu_lvx_125 + .las 雷达也判定完整", str(info["problems"]))

        # ---- 配置文件：可选 since=YYYYMMDD，只分析该日期及之后的数据集 ----
        cfg = base / "roots.txt"
        cfg.write_text(
            "# 注释\n/mnt/a\n/mnt/b since=20261021\n/mnt/b since=20261021\n",
            encoding="utf-8",
        )
        roots = module.load_roots(cfg)
        check([item["root"].name for item in roots] == ["a", "b"], "扫描路径配置去重", str(roots))
        check(roots[0]["since"] == "" and roots[1]["since"] == "20261021", "since 解析正确", str(roots))

        candidates = [
            base / "137000_E5-001_yanzhen_20261020",
            base / "137001_E5-001_yanzhen_20261021",
            base / "137002_E5-001_yanzhen_20261022",
        ]
        kept, skipped = module.filter_candidates(candidates, "20261021")
        check(len(kept) == 2 and skipped == 1, "since 过滤掉更早的数据集", f"{kept} / {skipped}")
        check(kept[0].name.endswith("20261021") and kept[-1].name.endswith("20261022"), "保留 1021 及之后")
        kept_all, skipped_all = module.filter_candidates(candidates, "")
        check(len(kept_all) == 3 and skipped_all == 0, "不配 since 时不过滤")

        # ---- 新增数据分析完立即推飞书（不真发，替换发送函数） ----
        import ap18_visualizer as viz

        sent: dict = {}
        original_send = viz._send_feishu_card
        try:
            viz._send_feishu_card = (
                lambda title, body, template="green": (
                    sent.update(title=title, body=body, template=template) or (True, "test-sent")
                )
            )
            ok = module.notify_new_datasets(
                [
                    {
                        "name": "137875_20261021",
                        "region": "南美洲地区",
                        "vehicle": "EX5-001",
                        "day": "20261021",
                        "analyzed_at": "2026-10-23 13:07:58",
                    }
                ]
            )
            check(
                ok and "137875_20261021" in sent["body"]
                and "数据采集时间：20261021" in sent["body"],
                "新增数据分析完立即推飞书（数据集一行 + 数据采集时间另起一行）",
                str(sent)[:300],
            )
            check("南美洲地区" in sent["body"] and "EX5-001" in sent["body"], "推送带项目/车型")
            check(
                sent.get("title", "").startswith("参数验证：新增数据集已分析 · EX5-001，请及时确认融合图效果"),
                "单个新增标题用车型 + 请及时确认融合图效果",
                sent.get("title", ""),
            )
            sent.clear()
            module.notify_new_datasets([])
            check(not sent, "没有新增数据不推送")
            sent.clear()
            module.notify_new_datasets(
                [
                    {"name": "a_20261021", "region": "欧洲地区", "vehicle": "LYNK01-001", "day": "20261021", "analyzed_at": "x"},
                    {"name": "b_20261021", "region": "南美洲地区", "vehicle": "EX5-001", "day": "20261021", "analyzed_at": "x"},
                ]
            )
            check(
                "新增 2 个数据集已分析" in sent.get("title", ""),
                "多个新增合并成一张卡片",
                sent.get("title", ""),
            )
            sent.clear()
            module.notify_new_datasets(
                [
                    {"name": "138123_20261022", "region": "欧洲地区", "vehicle": "LYNK01-001", "day": "20261022", "status": "existing", "analyzed_at": "x"},
                    {"name": "138128_20261023", "region": "欧洲地区", "vehicle": "LYNK01-001", "day": "20261023", "status": "existing", "analyzed_at": "x"},
                ]
            )
            check(
                "待确认" in sent.get("title", "") and "已有产出" in sent.get("title", ""),
                "平台已有产出的新数据集也会推送（标题注明已有产出）",
                sent.get("title", ""),
            )
            sent.clear()
            module.notify_failures(
                [
                    {
                        "name": "138900_E5-001_yanzhen_20261024",
                        "region": "欧洲地区",
                        "vehicle": "LYNK01-001",
                        "day": "20261024",
                        "error": "没有相机目录达到 10 帧",
                    }
                ]
            )
            check(
                "分析失败" in sent.get("title", "")
                and "没有相机目录达到 10 帧" in sent.get("body", ""),
                "分析失败也立即推飞书（红卡提醒检查）",
                sent.get("title", ""),
            )
        finally:
            viz._send_feishu_card = original_send

    print("结果：全部通过" if failed == 0 else f"结果：{failed} 项失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
