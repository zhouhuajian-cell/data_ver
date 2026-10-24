"""参数验证：目录预检单测（不需要 cv2/laspy，也不需要真实数据）。

跑法：python tests/test_verify.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import verifier  # noqa: E402

PASSED = 0
FAILED = 0


def check(ok: bool, label: str, detail=None) -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [ OK ] {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label}{f' - {detail}' if detail else ''}")


def build_dataset(root: Path) -> None:
    calib = root / "parameter_yaml" / "20250124"
    (calib / "extrinsic").mkdir(parents=True, exist_ok=True)
    (calib / "sensors_used.yaml").write_text(
        "calibration_date: 2025-01-24\n"
        "vehicle: 'E5-001'\n"
        "sensors: \n"
        "  front: camera-1, '/cam/st/x2f/front'\n"
        "  lidar: lidar-1, '/lidar/hs/pandar128/topmiddle'\n"
        "  body: 'body-2'\n",
        encoding="utf-8",
    )
    (calib / "topic_map.yaml").write_text(
        "topic:\n  camera-1: '/cam/st/x2f/front'\n  lidar-1: '/lidar/hs/pandar128/topmiddle'\n",
        encoding="utf-8",
    )
    (calib / "extrinsic" / "body-2_to_camera-1.yaml").write_text("x: 1\n", encoding="utf-8")

    cam = root / "cam_st_x2f_front"
    cam.mkdir()
    for index in range(3):
        (cam / f"179087400{index}.jpg").write_bytes(b"jpgdata")
    lidar = root / "lidar_hs_pandar128_topmiddle"
    lidar.mkdir()
    (lidar / "1790874001.007.las").write_bytes(b"lasdata")
    pose = root / "rtkimu_qj_7683"
    pose.mkdir()
    (pose / "Vehicle(imu1_2_body2).pose").write_text("timestamp x y z\n", encoding="utf-8")


def test_probe_ready(tmp: Path) -> None:
    print("[1] 目录预检（结构齐全）")
    root = tmp / "dataset_ok"
    root.mkdir()
    build_dataset(root)
    info = verifier.probe_dataset(root)
    check(info["ready"] is True, "预检通过", info.get("problems"))
    check(info["parameter_yaml"] is True, "识别 parameter_yaml")
    check(info["calib_date_folder"] == "20250124", "识别标定日期目录", info.get("calib_date_folder"))
    check(info["sensors_yaml"] and info["topic_map_yaml"], "识别 sensors_used/topic_map")
    check(info["cameras"] == ["cam_st_x2f_front"], "按 sensors_used 推导相机目录", info.get("cameras"))
    check(info["camera_image_counts"].get("cam_st_x2f_front") == 3, "统计相机图片数")
    check(info["lidars"] == ["lidar_hs_pandar128_topmiddle"], "识别雷达目录", info.get("lidars"))
    check(bool(info["pose_files"]), "找到 .pose 位姿文件", info.get("pose_files"))
    check(info["has_front_camera"] is True, "识别 front 相机昵称")
    check(info["problems"] == [], "没有阻塞问题", info.get("problems"))


def test_probe_missing(tmp: Path) -> None:
    print("[2] 目录预检（缺参数目录）")
    root = tmp / "dataset_bad"
    root.mkdir()
    (root / "cam_st_x2f_front").mkdir()
    info = verifier.probe_dataset(root)
    check(info["ready"] is False, "缺 parameter_yaml 不可验证")
    check(any("parameter_yaml" in item for item in info["problems"]), "问题里提到 parameter_yaml", info.get("problems"))
    missing = verifier.probe_dataset(tmp / "not_exists")
    check(missing["ready"] is False, "不存在的目录不可验证")
    check(any("不存在" in item for item in missing["problems"]), "提示目录不存在", missing.get("problems"))


def test_resolve(tmp: Path) -> None:
    print("[3] 路径解析")
    root = tmp / "dataset_ok"
    check(verifier.resolve_data_root(str(root)) == root, "本地路径原样返回")
    check(verifier.resolve_data_root(f'"{root}"') == root, "去掉用户粘贴的引号")


def test_frontend_wiring() -> None:
    print("[4] 前端接线")
    web = Path(__file__).resolve().parent.parent / "web"
    html = (web / "mileage.html").read_text(encoding="utf-8")
    js = (web / "verify.js").read_text(encoding="utf-8")
    check('id="tabVerify"' in html and 'id="sheetVerify"' in html, "页面有参数验证 sheet 与入口")
    check("verify.js" in html, "页面加载 verify.js")
    check("/api/verify/run" in js and "/api/verify/task" in js, "前端调用验证接口")
    check('id="verifyRunButton"' in html, "有开始验证按钮")
    check(
        "/api/verify/overview" in js and "/api/verify/confirm" in js,
        "前端调用交付图浏览/确认接口",
    )
    check(
        'id="verifyConfirmPanel"' in html and 'verify-package' in js,
        "页面有交付图确认与交付包下载面板",
    )
    check(
        "/api/verify/scan" in js and 'id="verifyBatchPanel"' in html,
        "前端有批量验证（父目录遍历）",
    )
    check(
        "imageLightbox" in js and "Escape" in js,
        "图片支持放大查看（双击/Esc 返回）",
    )
    check(
        all(tag in html for tag in (
            'id="tabSingle"', 'id="tabBatch"', 'id="tabVerify"', 'id="tabBatchVerify"',
            'id="sheetSingle"', 'id="sheetBatch"', 'id="sheetVerify"', 'id="sheetBatchVerify"',
        )),
        "页面有四个 sheet（单日/批量里程、单日/批量参数验证）",
    )
    mileage_js = (web / "mileage.js").read_text(encoding="utf-8")
    check("batchVerify" in mileage_js, "批量参数验证独立 sheet 已接线")
    dash_js = (web / "dashboard.js").read_text(encoding="utf-8")
    check(
        'id="tabOverseasDash"' in html and 'id="sheetOverseasDash"' in html
        and 'id="dashMileageGauge"' in html and 'id="dashVerifyGauge"' in html,
        "顶部有整合后的「海外数据看板」（里程 + 参数验证 + 仪表盘）",
    )
    check(
        "/api/dashboard/mileage" in dash_js and "/api/dashboard/verify" in dash_js,
        "看板前端调用里程/参数验证看板接口",
    )
    check(
        'id="dashTimeFilter"' in html and "date_from=" in dash_js
        and "this-month" in html and "last-month" in html,
        "看板时间筛选支持 全部/本月/上月/自定义范围",
    )
    check(
        'id="tabVerifyLib"' not in html and 'id="verifyLibTable"' not in html,
        "参数验证库独立 sheet 已移除（库仍在单日/批量验证里）",
    )
    check(
        'id="verifyForceRunButton"' in html and 'id="verifyForceRerunButton"' in html
        and 'id="verifyRemoveButton"' in html
        and 'id="verifyBatchForce"' in html and "refresh: Boolean(force)" in js,
        "参数验证支持强制重新分析（忽略数据库缓存）",
    )
    check(
        all(tag not in html for tag in (
            "verifyOptReproject", "verifyOptForceFramemap", "verifyForceRefresh",
            "verifyFrameStart", "verifyFrameStep", "verifyBatchReproject", "verifyBatchForceRefresh",
        )),
        "参数验证选项已精简（去掉重投影/FrameMap/重验/帧参数）",
    )
    check(
        'verifyOptQuality' in html and 'verifyOptProjection' in html,
        "保留数据质检与外参校验开关",
    )
    check("不通过" in js and "退回" not in js, "交付图支持通过/不通过两种判定")
    check(
        all(tag not in html for tag in (
            "verifyResult", "verifyVerdict", "verifyProbeTable", "verifySummary",
            "verifyCamerasTable", "verifyArtifactsTable", "verifyLog", "verifyImages",
        )),
        "不展示自动结论/数据集/相机表/产出表/日志",
    )
    check("结论" not in html and "结论" not in js, "结论由人给出，页面不输出自动结论")
    check('id="verifyHistoryPanel"' in html and "/api/verify/history" in js, "单日参数验证有「库中已有结果」列表")
    check(
        'id="verifyBatchHistoryPanel"' in html and "historyViews" in js,
        "批量参数验证也有「库中已有结果」列表（可直接点开确认）",
    )
    check(
        "/api/verify/notify" in js and "verifyNotifyMask" in js,
        "全部评完后有「是否发送参数验证结果」弹窗",
    )
    check(
        'id="globalRegionFilter"' in html and 'id="todoBar"' in html and "/api/todo" in mileage_js,
        "顶部有项目全局筛选 + 各项目待办",
    )
    check(
        all(tag in html for tag in (
            "historyTimeFilter", "verifyHistoryTimeFilter", "verifyBatchHistoryTimeFilter",
        ))
        and "this-month" in html and "last-month" in html and "custom" in html
        and "date_from=" in mileage_js
        and "date_from=" in js,
        "库中已有结果支持时间范围筛选（本月/上月/自定义）",
    )
    check(
        all(tag not in html for tag in (
            "historyRegionFilter", "verifyHistoryRegion", "verifyBatchHistoryRegion",
        )),
        "库中结果不再单独放项目筛选（统一用顶部全局筛选）",
    )
    check(
        'id="batchHistoryPanel"' in html and "batchHistoryTimeFilter" in html
        and "loadAllMileageHistory" in mileage_js,
        "批量里程也有「库中已有结果」列表",
    )
    check(
        "verifyHistoryReviewFilter" in html and "verifyBatchHistoryReviewFilter" in html
        and "review=" in js,
        "参数验证历史支持「已验证/未验证」筛选",
    )
    check(
        "单日参数验证（外参校验" not in html,
        "单日参数验证标题去掉括注",
    )
    check(
        html.index('id="verifyConfirmPanel"') < html.index('id="verifyHistoryPanel"'),
        "分析结果（交付图确认）排在库中结果上面",
    )
    check(
        "投影图按时间戳均匀抽 5 张" not in html,
        "交付图确认面板的说明文字已删除",
    )


def test_scan_parent(tmp: Path) -> None:
    print("[7] 父目录自动遍历（批量验证）")
    parent = tmp / "scan_parent"
    dataset_a = parent / "137657_E5-001_yanzhen_20261017"
    (dataset_a / "parameter_yaml" / "20260920").mkdir(parents=True)
    dataset_b = parent / "137501_E5-001_yanzhen_20261001"
    (dataset_b / "framemap_dataquality_projection" / "data_quality").mkdir(parents=True)
    (parent / "not_a_dataset").mkdir()

    result = verifier.scan_verify_datasets(parent)
    check(result["count"] == 2, "扫描到 2 个数据集", str(result["count"]))
    check(result["ready_count"] == 1, "可验证 1 个（有 parameter_yaml）", str(result["ready_count"]))
    check(result["analyzed_count"] == 1, "已有产出 1 个", str(result["analyzed_count"]))
    paths = [item["path"] for item in result["datasets"]]
    check(str(dataset_a) in paths and str(dataset_b) in paths, "路径返回正确", str(paths))

    empty = verifier.scan_verify_datasets(parent / "not_a_dataset")
    check(empty["count"] == 0, "空目录返回 0 个")


def test_http_api(tmp: Path) -> None:
    """真起一个 HTTP 服务，验证 /api/verify/* 路由接线。"""
    print("[5] HTTP 接口")
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    import ap18_visualizer as viz

    server = ThreadingHTTPServer(("127.0.0.1", 0), viz.make_handler())
    server.request_logging = False  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        def post(body: dict, path: str) -> tuple[int, dict]:
            request = urllib.request.Request(
                f"{base}{path}",
                data=json.dumps(body).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.status, json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode())

        status, payload = post({"data_root": str(tmp / "dataset_ok")}, "/api/verify/probe")
        check(status == 200 and payload.get("ok") is True, "probe 接口 200", f"status={status}")
        check(payload["dataset"]["ready"] is True, "probe 返回 ready=true")
        check(payload["dataset"]["cameras"] == ["cam_st_x2f_front"], "probe 返回相机目录")

        status, payload = post({"data_root": str(tmp / "not_exists")}, "/api/verify/probe")
        check(status == 200 and payload["dataset"]["ready"] is False, "probe 不存在目录返回 ready=false")

        status, payload = post({}, "/api/verify/probe")
        check(status == 400 and "data_root" in payload.get("error", ""), "probe 缺参数返回 400")

        status, scan = post({"data_root": str(tmp)}, "/api/verify/scan")
        paths = [item.get("path") for item in scan.get("datasets", [])]
        check(
            status == 200 and str(tmp / "dataset_ok") in paths,
            "scan 接口识别父目录下的数据集",
            f"status={status} paths={paths}",
        )
        target = next(item for item in scan["datasets"] if item.get("path") == str(tmp / "dataset_ok"))
        check(target.get("has_parameter_yaml") is True, "scan 返回数据集可验证标记")

        with urllib.request.urlopen(f"{base}/api/verify/tasks", timeout=30) as response:
            tasks = json.loads(response.read().decode())
        check(tasks.get("ok") is True and isinstance(tasks.get("tasks"), list), "tasks 接口正常")

        with urllib.request.urlopen(f"{base}/mileage.html", timeout=30) as response:
            html = response.read().decode()
        check("参数验证" in html and "verify.js" in html, "页面含参数验证 sheet")
    finally:
        server.shutdown()
        server.server_close()


def build_verify_output(root: Path) -> None:
    """造一份已有验证产出：12 张投影（两路）+ 3 张质检图。"""
    base = root / "framemap_dataquality_projection"
    (base / "projection" / "front").mkdir(parents=True)
    (base / "projection" / "front7").mkdir(parents=True)
    (base / "data_quality").mkdir(parents=True)
    for index in range(12):
        stamp = f"1792230590.{100 + index}"
        (base / "projection" / "front" / f"{stamp}.jpg").write_bytes(b"JPG-FRONT")
        (base / "projection" / "front7" / f"{stamp}.jpg").write_bytes(b"JPG-FRONT7")
    for name in ("sensor_timeline.png", "file_size_boxplot.png", "clips_count_bar.png"):
        (base / "data_quality" / name).write_bytes(b"PNG-DATA")


def test_confirm_and_delivery(tmp: Path) -> None:
    print("[6] 交付图浏览 / 人工确认 / 交付包下载")
    import json
    import threading
    import urllib.error
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer

    import mileage_store
    import ap18_visualizer as viz

    dataset = tmp / "137657_E5-001_yanzhen_20261017"
    dataset.mkdir()
    build_verify_output(dataset)

    # 确认状态写临时库，避免污染仓库 data/mileage.db
    mileage_store.DB_PATH = tmp / "verify_store.db"
    mileage_store._initialized = False

    # 自动验证的新数据：数据目录里没有 framemap，产出在任务目录 → 验证状态应为「待确认」而不是 —
    auto_root = tmp / "138900_E5-001_yanzhen_20261021"
    auto_root.mkdir()
    auto_out = tmp / "auto_138900_E5-001_yanzhen_20261021"
    (auto_out / "projection" / "front").mkdir(parents=True)
    (auto_out / "projection" / "front" / "1792572424.121.jpg").write_bytes(b"JPG-AUTO")
    auto_state = viz._verify_review_state(
        {"data_root": str(auto_root), "output_dir": str(auto_out)}
    )
    check(
        bool(auto_state) and auto_state.get("state") == "pending" and auto_state.get("total") == 1,
        "产出在任务目录时验证状态为「待确认」",
        str(auto_state),
    )

    delivery_dir = tmp / "deliveries"
    delivery_dir.mkdir()
    zip_name = "137657_E5-001_yanzhen_20261017.zip"
    (delivery_dir / zip_name).write_bytes(b"PK\x03\x04fake-zip")
    (delivery_dir / "137657_E5-001_yanzhen_20261017.json").write_text(
        json.dumps({"data_root": str(dataset), "date": "20261017", "title": "E5-001 参数验证"},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    old_delivery_root = viz.VERIFY_DELIVERY_ROOT
    viz.VERIFY_DELIVERY_ROOT = delivery_dir

    server = ThreadingHTTPServer(("127.0.0.1", 0), viz.make_handler())
    server.request_logging = False  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    first = "projection/front/1792230590.100.jpg"
    last = "projection/front/1792230590.111.jpg"

    def post(body: dict, path: str) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{base}{path}", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def get(path: str, raw: bool = False):
        try:
            with urllib.request.urlopen(f"{base}{path}", timeout=60) as response:
                return response.status, (response.read() if raw else json.loads(response.read().decode()))
        except urllib.error.HTTPError as exc:
            return exc.code, None

    try:
        status, payload = post({"data_root": str(dataset)}, "/api/verify/overview")
        check(status == 200 and payload.get("ok") is True, "overview 接口 200", f"status={status}")
        groups = {group["key"]: group for group in payload.get("groups", [])}
        check(set(groups) == {"projection_front", "projection_front7", "quality"}, "分组齐全", str(list(groups)))
        front = groups["projection_front"]
        check(front["total"] == 12 and len(front["items"]) == 5, "投影图均匀抽 5 张", f"{front['total']}/{len(front['items'])}")
        names = [item["name"] for item in front["items"]]
        check(names[0] == first and names[-1] == last, "抽样首尾包含最早/最晚帧", str(names[:2]))
        times = [item["time"] for item in front["items"]]
        check(all(times), "抽样项带时间戳文本", str(times))
        check(len(groups["quality"]["items"]) == 3, "质检图全量展示", str(len(groups["quality"]["items"])))
        check(
            all("name" in item and "time" in item for item in front["items"]),
            "投影图条目带文件名/时间",
            str(front["items"][:1]),
        )

        # ---- 重新抽取：投影图可随机重抽 5 张（避开空旷路段） ----
        shown = [item["name"] for item in front["items"]]
        status, sample = post(
            {"data_root": str(dataset), "category": "projection_front", "exclude": shown},
            "/api/verify/resample",
        )
        check(status == 200 and sample.get("ok") is True, "重新抽取接口 200", f"status={status}")
        picked = [item["name"] for item in sample.get("items", [])]
        check(len(picked) == 5, "重新抽取 5 张", str(picked))
        check(not (set(picked) & set(shown)), "重新抽取避开当前已显示的 5 张", str(picked))
        picked_times = [item.get("time") for item in sample.get("items", [])]
        check(all(picked_times) and picked_times == sorted(picked_times), "重新抽取结果按时间排序", str(picked_times))
        check(sample.get("total") == 12, "重新抽取返回总张数", str(sample.get("total")))

        status, _ = post(
            {"data_root": str(dataset), "category": "quality"},
            "/api/verify/resample",
        )
        check(status == 400, "质检图不支持重新抽取", f"status={status}")

        status, raw = get(f"/api/verify/source?data_root={urllib.parse.quote(str(dataset))}"
                          f"&path={urllib.parse.quote(first)}", raw=True)
        check(status == 200 and raw == b"JPG-FRONT", "图片可通过 source 接口读取", str(status))
        status, _ = get(f"/api/verify/source?data_root={urllib.parse.quote(str(dataset))}"
                        "&path=../../etc/passwd")
        check(status == 403, "source 接口拒绝路径穿越", f"status={status}")

        status, payload = post(
            {"data_root": str(dataset), "category": "projection_front",
             "names": [first, last], "status": "confirmed"},
            "/api/verify/confirm",
        )
        check(status == 200 and payload.get("ok") is True, "批量确认 200", f"status={status}")
        check(payload.get("summary", {}).get("confirmed") == 2, "确认状态已落库（confirmed=2）",
              str(payload.get("summary")))

        status, payload = get(f"/api/verify/confirmations?data_root={urllib.parse.quote(str(dataset))}")
        check(status == 200 and len(payload.get("items", {})) == 2, "确认状态可读回", f"status={status}")

        # ---- 不通过必须填写问题原因（前后端都拦） ----
        status, payload = post(
            {"data_root": str(dataset), "category": "projection_front",
             "names": [first], "status": "rejected"},
            "/api/verify/confirm",
        )
        check(status == 400, "不通过未填原因被拒绝", f"status={status} {payload}")

        status, payload = post(
            {"data_root": str(dataset), "category": "projection_front",
             "names": [first], "status": "rejected", "note": "投影错位"},
            "/api/verify/confirm",
        )
        check(status == 200, "填写原因后可标记不通过", f"status={status} {payload}")
        check(
            ((payload.get("items") or {}).get(f"projection_front|{first}") or {}).get("note") == "投影错位",
            "不通过原因已落库并可读回",
            str((payload.get("items") or {}).get(f"projection_front|{first}")),
        )

        status, payload = post(
            {"data_root": str(dataset), "category": "projection_front",
             "names": [first], "status": "pending"},
            "/api/verify/confirm",
        )
        check(payload.get("summary", {}).get("confirmed") == 1, "取消确认后只剩 1 条", str(payload.get("summary")))

        # ---- 飞书推送：全部评完后先弹窗确认，再调 notify 真正发送 ----
        import os as _os
        sink = tmp / "feishu.log"
        _os.environ["AP18_FEISHU_WEBHOOK_FILE"] = str(sink)
        last_payload = None
        for key, group in groups.items():
            status, resp = post(
                {
                    "data_root": str(dataset),
                    "category": key,
                    "names": [item["name"] for item in group["items"]],
                    "status": "confirmed",
                },
                "/api/verify/confirm",
            )
            last_payload = resp
        note = (last_payload or {}).get("notification") or {}
        check(note.get("pending") is True, "全部评完后返回待发送汇总（等用户确认）", str(note))
        check(
            note.get("passed") == 13 and note.get("rejected") == 0,
            "弹窗数据含通过/不通过数量",
            str(note),
        )
        check(
            not sink.is_file() or not sink.read_text(encoding="utf-8").strip(),
            "用户确认前不自动推送飞书",
        )

        status, resp = post({"data_root": str(dataset)}, "/api/verify/notify")
        sent = resp.get("notification") or {}
        check(status == 200 and sent.get("sent") is True, "确认后发送飞书", str(sent))
        sink_text = sink.read_text(encoding="utf-8") if sink.is_file() else ""
        first_message = json.loads(sink_text.strip().splitlines()[0]) if sink_text.strip() else {}
        first_text = feishu_payload_text(first_message)
        first_title = first_text.splitlines()[0]
        check(
            first_title.startswith("【车辆参数验证】") and "参数验证通过" in first_title,
            "飞书标题为【车辆参数验证】地区 · 车辆 · 结果",
            first_title,
        )
        check(
            "数据采集时间" in first_text and "2026-10-17" in first_text,
            "正文有数据采集时间（源数据日期）",
            first_text[:200],
        )
        check(
            (first_message.get("card") or {}).get("header", {}).get("template") == "green",
            "通过时卡片标题栏为绿色",
            str(first_message)[:120],
        )
        check(dataset.name in sink_text, "飞书消息包含数据集与汇总")

        status, resp = post(
            {
                "data_root": str(dataset),
                "category": "quality",
                "names": [groups["quality"]["items"][0]["name"]],
                "status": "confirmed",
            },
            "/api/verify/confirm",
        )
        check(
            ((resp.get("notification") or {}).get("reason")) == "结果未变化，无需重复推送",
            "结论未变化时不重复发送",
            str(resp.get("notification")),
        )

        # 改成一张「不通过」：结论变化 -> 再次待发送，确认后再发
        status, resp = post(
            {
                "data_root": str(dataset),
                "category": "quality",
                "names": [groups["quality"]["items"][0]["name"]],
                "status": "rejected",
                "note": "质检图异常",
            },
            "/api/verify/confirm",
        )
        note2 = resp.get("notification") or {}
        check(note2.get("pending") is True, "改判后重新弹窗待发送", str(note2))
        check(note2.get("rejected") == 1, "弹窗统计不通过数量", str(note2))
        status, resp = post({"data_root": str(dataset)}, "/api/verify/notify")
        sent2 = resp.get("notification") or {}
        check(sent2.get("sent") is True, "不通过结果确认后也会发送", str(sent2))
        lines = sink.read_text(encoding="utf-8").strip().splitlines()
        last_message = json.loads(lines[-1]) if lines else {}
        last_text = feishu_payload_text(last_message)
        last_title = last_text.splitlines()[0]
        check(
            len(lines) >= 2 and "不通过" in last_text and "通过" in last_text,
            "推送消息包含通过/不通过汇总",
            last_text[:200],
        )
        check("参数验证不通过" in last_title, "有不通过时标题为「参数验证不通过」", last_title)
        check(
            (last_message.get("card") or {}).get("header", {}).get("template") == "red",
            "有不通过时卡片标题栏为红色",
            str(last_message)[:120],
        )
        check(
            "不通过" in last_text and "数据质检" in last_text and ".jpg" not in last_text,
            "不通过只写哪一项，不列具体图片名",
            last_text[:200],
        )

        status, payload = get("/api/verify/deliveries")
        check(status == 200 and len(payload.get("items", [])) == 1, "交付包列表 1 个", f"status={status}")
        entry = payload["items"][0]
        check(entry.get("date") == "20261017", "交付包按日期展示", str(entry.get("date")))
        check(entry.get("data_root") == str(dataset), "交付包带数据目录", str(entry.get("data_root")))
        status, raw = get(entry["download_url"], raw=True)
        check(status == 200 and raw == b"PK\x03\x04fake-zip", "交付包可下载", str(status))
        status, _ = get("/api/verify/delivery?name=..%2F..%2Fetc%2Fpasswd")
        check(status == 403, "交付包下载拒绝路径穿越", f"status={status}")

        # ---- 历史命中：验证过的目录直接读库 ----
        mileage_store.save_verify_run(
            str(dataset),
            {
                "task_id": "cached-test",
                "status": "success",
                "verdict": "外参验证通过：2/2 个相机内参+外参齐全",
                "dataset": {"data_root": str(dataset)},
                "summary": {"camera_total": 2, "camera_ok": 2},
                "artifacts": [{"name": "sensor_status_report.json", "size": 100, "kind": "json"}],
                "log": ["cached"],
            },
            str(dataset / "framemap_dataquality_projection"),
        )
        status, payload = post({"data_root": str(dataset)}, "/api/verify/run")
        check(status == 200 and payload.get("cached") is True, "重复验证命中历史库", f"status={status}")
        # 交付包按天放到数据目录 delivery_package 下（不占服务器盘）
        status, pkg = post({"data_root": str(dataset)}, "/api/verify/package")
        check(status == 200 and pkg.get("ok") is True and pkg.get("size"), "生成交付包到数据目录", str(pkg))
        pkg_path = dataset / "delivery_package" / pkg.get("name", "")
        check(pkg_path.is_file(), "交付包文件位于数据目录下", str(pkg_path))
        status, raw_zip = get(pkg.get("url", ""), raw=True)
        check(status == 200 and raw_zip[:2] == b"PK", "按数据目录下载交付包", str(status))
        status, delivery_list = get("/api/verify/deliveries")
        check(
            any(item.get("data_root") and item.get("name") == pkg.get("name") for item in delivery_list.get("items", [])),
            "交付包列表包含数据目录包",
        )


        status, history = get("/api/verify/history")
        check(status == 200, "库中已有结果接口可用", f"status={status}")
        names = [item.get("name") for item in history.get("items", [])]
        check(dataset.name in names, "历史列表包含验证过的目录", str(names))
        check(history.get("total", 0) >= 1, "历史列表返回总数", str(history.get("total")))
        status, filtered = get("/api/verify/history?q=" + dataset.name)
        check(
            filtered.get("total") == 1 and filtered["items"][0]["name"] == dataset.name,
            "历史列表支持筛选",
        )
        # ---- 参数验证库：按验证通过 / 不通过筛选 ----
        status, passed = get("/api/verify/history?review=passed&limit=50")
        check(status == 200, "参数验证库支持「验证通过」筛选", f"status={status}")
        check(
            all(
                (item.get("review") or {}).get("state") == "done"
                and int((item.get("review") or {}).get("rejected") or 0) == 0
                for item in passed.get("items", [])
            ),
            "「验证通过」只返回没有不通过的记录",
            str(passed.get("items"))[:200],
        )
        status, rejected = get("/api/verify/history?review=rejected&limit=50")
        check(status == 200, "参数验证库支持「验证不通过」筛选", f"status={status}")
        check(
            all(
                int((item.get("review") or {}).get("rejected") or 0) > 0
                for item in rejected.get("items", [])
            ),
            "「验证不通过」只返回有不通过的记录",
            str(rejected.get("items"))[:200],
        )

        # ---- 海外数据看板：里程 / 参数验证 ----
        status, dash_mileage = get("/api/dashboard/mileage?days=90")
        check(status == 200 and dash_mileage.get("ok") is True, "里程看板接口 200", f"status={status}")
        check(
            "effective_m" in dash_mileage and isinstance(dash_mileage.get("regions"), list),
            "里程看板返回总览与项目列表",
            str(list(dash_mileage))[:200],
        )
        status, dash_verify = get("/api/dashboard/verify?days=90")
        check(status == 200 and dash_verify.get("ok") is True, "参数验证看板接口 200", f"status={status}")
        totals = dash_verify.get("totals") or {}
        check(
            all(key in totals for key in ("datasets", "done", "passed", "rejected", "pending", "pass_rate_pct")),
            "参数验证看板返回通过/不通过/待确认统计",
            str(totals),
        )
        check(
            all("region" in item and "passed" in item for item in dash_verify.get("regions", [])),
            "参数验证看板按项目汇总",
            str(dash_verify.get("regions"))[:200],
        )

        review_entry = next(
            (item for item in history.get("items", []) if item["name"] == dataset.name), None
        )
        page_region = mileage_store.list_verify_runs_page(region="独联体地区", limit=100)
        check(
            page_region["total"] == 1
            and page_region["items"][0]["region"] == "独联体地区"
            and page_region["items"][0]["vehicle"] == "E5-001",
            "数据库 region/vehicle 字段筛选",
            str(page_region["items"][0])[:200],
        )
        # 时间筛选：造一条 2020 年的旧记录，days=30 应排除、days=0 应包含
        old_root = tmp / "legacy" / "20200101_sample"
        mileage_store.save_verify_run(
            str(old_root),
            {"status": "success", "task_id": "old", "dataset": {"data_root": str(old_root)}},
            "",
            updated_at="2020-01-01 00:00:00",
        )
        recent = mileage_store.list_verify_runs_page(days=30, limit=100)
        all_runs = mileage_store.list_verify_runs_page(limit=100)
        check(
            all(item["name"] != old_root.name for item in recent["items"]),
            "最近 30 天排除旧记录",
            str([item["name"] for item in recent["items"]]),
        )
        check(
            any(item["name"] == old_root.name for item in all_runs["items"]),
            "全部时间包含旧记录",
        )
        status, recent_api = get("/api/verify/history?days=30&limit=100")
        check(
            all(item["name"] != old_root.name for item in recent_api.get("items", [])),
            "历史接口支持 days 参数",
        )
        # 按具体数据日期筛选（不是分析/验证时间）
        status, date_api = get("/api/verify/history?date=2026-10-17&limit=100")
        check(
            status == 200
            and date_api.get("total") == 1
            and date_api["items"][0]["name"] == dataset.name,
            "参数验证历史支持按具体日期筛选",
            str(date_api.get("total")),
        )
        status, date_old = get("/api/verify/history?date=2020-01-01&limit=100")
        check(
            status == 200
            and date_old.get("total") == 1
            and date_old["items"][0]["name"] == old_root.name,
            "按日期筛选只命中当天数据",
            str(date_old.get("total")),
        )
        # 里程历史同样按数据日期筛选
        mileage_store.save_analysis(
            str(tmp / "mileage_day" / "20261017"),
            {"mileage": {"effective_m": 1000.0, "gps_stable_m": 2000.0}},
        )
        status, mileage_date = get("/api/mileage/history?date=2026-10-17&limit=100")
        check(
            status == 200
            and any(item.get("day") == "20261017" for item in mileage_date.get("items", [])),
            "里程历史支持按具体日期筛选",
            str(mileage_date.get("total")),
        )
        status, mileage_empty = get("/api/mileage/history?date=2020-01-01&limit=100")
        check(status == 200 and mileage_empty.get("total") == 0, "里程历史按日期筛选排除其他日期")
        # 范围筛选（本月/上月/自定义走后端 date_from/date_to）
        status, range_api = get("/api/verify/history?date_from=2026-10-17&date_to=2026-10-17&limit=100")
        check(
            status == 200 and range_api.get("total") == 1,
            "参数验证历史支持按日期范围筛选",
            str(range_api.get("total")),
        )
        mileage_store.save_analysis(
            str(tmp / "mileage_day2" / "20261015"),
            {"mileage": {"effective_m": 500.0, "gps_stable_m": 1000.0}},
        )
        status, mileage_range = get("/api/mileage/history?date_from=2026-10-15&date_to=2026-10-19&limit=100")
        check(
            status == 200 and mileage_range.get("total") == 2,
            "里程历史支持按日期范围筛选",
            str(mileage_range.get("total")),
        )
        status, mileage_out = get("/api/mileage/history?date_from=2026-10-20&date_to=2026-10-25&limit=100")
        check(status == 200 and mileage_out.get("total") == 0, "日期范围之外不返回")
        # 看板也支持日期范围（本月/上月/自定义走同一套 date_from/date_to）
        status, dash_mileage_range = get("/api/dashboard/mileage?date_from=2026-10-15&date_to=2026-10-17")
        check(
            status == 200 and dash_mileage_range.get("days") == 2,
            "里程看板按日期范围筛选",
            str(dash_mileage_range.get("days")),
        )
        status, dash_verify_range = get("/api/dashboard/verify?date_from=2026-10-17&date_to=2026-10-17")
        check(
            status == 200 and (dash_verify_range.get("totals") or {}).get("datasets") == 1,
            "参数验证看板按日期范围筛选",
            str(dash_verify_range.get("totals")),
        )
        status, done_api = get("/api/verify/history?review=done&limit=100")
        status, pending_api = get("/api/verify/history?review=pending&limit=100")
        check(
            done_api.get("total") >= 1
            and all((item.get("review") or {}).get("state") == "done" for item in done_api.get("items", [])),
            "按参数验证状态筛选：已验证",
            str(done_api.get("total")),
        )
        check(
            all((item.get("review") or {}).get("state") != "done" for item in pending_api.get("items", [])),
            "按参数验证状态筛选：未验证",
            str(pending_api.get("total")),
        )
        # 按数据日期倒序（20261017、20261001、20200101…）
        import re as _re
        status, ordered = get("/api/verify/history?limit=100")
        ordered_days = [
            _re.search(r"(20\d{6})", item.get("name") or "").group(1)
            for item in ordered.get("items", [])
            if _re.search(r"(20\d{6})", item.get("name") or "")
        ]
        check(
            ordered_days == sorted(ordered_days, reverse=True),
            "库中结果按数据日期倒序",
            str(ordered_days[:5]),
        )
        # 数据目录已不存在的历史记录可手动移除（确认面板「移除无效记录」）
        status, removed = post({"data_root": str(old_root)}, "/api/verify/remove")
        check(
            status == 200 and removed.get("ok") is True and removed.get("removed") is True,
            "可移除无效的验证记录",
            str(removed),
        )
        status, after_remove = get("/api/verify/history?limit=100")
        check(
            all(item["name"] != old_root.name for item in after_remove.get("items", [])),
            "移除后历史列表不再出现",
        )
        review = (review_entry or {}).get("review") or {}
        check(review.get("state") == "done", "历史列表带参数验证状态（已评完）", str(review))
        check(
            review.get("rejected") == 1 and review.get("passed", 0) >= 1,
            "历史列表参数验证统计正确",
            str(review),
        )
        check(
            (review_entry or {}).get("vehicle") == "E5-001"
            and (review_entry or {}).get("region") == "独联体地区",
            "历史列表带车辆/地区标注",
            str(review_entry)[:200],
        )
        check(
            bool((review_entry or {}).get("notified_at")),
            "历史列表带推送时间（结果推送时间）",
            str((review_entry or {}).get("notified_at")),
        )
        check(
            bool((review_entry or {}).get("verified_at")),
            "历史列表带验证时间（评完取最后一次确认时间）",
            str((review_entry or {}).get("verified_at")),
        )
        # 「确认」留痕：验证时间记到平台；同一数据多次确认以最新时间为准
        status, reviewed = post({"data_root": str(dataset)}, "/api/verify/review")
        check(
            status == 200 and reviewed.get("ok") is True and reviewed.get("updated_at"),
            "点击确认后平台记录验证时间",
            str(reviewed),
        )
        status, history_after = get("/api/verify/history?q=" + dataset.name)
        entry_after = (history_after.get("items") or [{}])[0]
        check(
            entry_after.get("verified_at") == reviewed.get("updated_at"),
            "历史列表验证时间取最新确认时间",
            str(history_after)[:300],
        )
        mileage_store.save_verify_review(
            str(dataset),
            status="done",
            total=15,
            passed=14,
            rejected=1,
            updated_at="2020-01-01 00:00:00",
        )
        status, reviewed2 = post({"data_root": str(dataset)}, "/api/verify/review")
        status, history_after2 = get("/api/verify/history?q=" + dataset.name)
        entry_after2 = (history_after2.get("items") or [{}])[0]
        check(
            entry_after2.get("verified_at") == reviewed2.get("updated_at")
            and entry_after2.get("verified_at") != "2020-01-01 00:00:00",
            "多次确认以最新确认时间为准",
            str(entry_after2.get("verified_at")),
        )
        # 没评完的记录也要有验证时间（回退到分析完成时间）
        status, pending_rows = get("/api/verify/history?review=pending&limit=50")
        if pending_rows.get("items"):
            check(
                all(not item.get("verified_at") for item in pending_rows.get("items", [])),
                "未评完记录不显示验证时间",
                str([item.get("verified_at") for item in pending_rows.get("items", [])][:3]),
            )
        for region_name, expected in (("独联体地区", 1), ("欧洲地区", 0)):
            status, filtered_region = get(
                "/api/verify/history?region=" + urllib.parse.quote(region_name)
            )
            check(
                filtered_region.get("total") == expected,
                f"按地区筛选（{region_name}）",
                str(filtered_region.get("total")),
            )
        status, todo = get("/api/todo")
        check(status == 200 and todo.get("ok") is True, "待办接口可用", f"status={status}")
        todo_regions = {item["region"]: item for item in todo.get("regions", [])}
        check("独联体地区" in todo_regions, "待办按项目分区", str(list(todo_regions)))
        check(
            "pending_confirm" in todo_regions.get("独联体地区", {})
            and "mileage_days" in todo_regions.get("独联体地区", {}),
            "待办含待确认/里程数据",
            str(todo_regions.get("独联体地区"))[:200],
        )
        check(
            payload.get("task", {}).get("verdict") == "外参验证通过：2/2 个相机内参+外参齐全",
            "历史结果完整返回",
        )
        check(
            bool((review_entry or {}).get("delivery_url")),
            "历史列表带交付包下载链接",
            str((review_entry or {}).get("delivery_url")),
        )
        check(bool(payload.get("computed_at")), "历史结果带计算时间")
        status, scan = post({"data_root": str(tmp)}, "/api/verify/scan")
        entry = next(item for item in scan["datasets"] if item["path"] == str(dataset))
        check(entry.get("analyzed") is True, "批量扫描标注「已分析」")
        status, payload = post({"data_root": str(dataset), "refresh": True}, "/api/verify/run")
        # 强制重验不会走缓存（这里只断言没有 cached 标记；真实管线在后台线程跑，可能失败也无妨）
        check(payload.get("cached") is not True, "强制重验跳过历史库")
    finally:
        viz.VERIFY_DELIVERY_ROOT = old_delivery_root
        server.shutdown()
        server.server_close()


def feishu_payload_text(payload: dict) -> str:
    """兼容文本消息与卡片消息：取出标题+正文用于断言。"""
    if payload.get("msg_type") == "interactive":
        card = payload.get("card") or {}
        title = ((card.get("header") or {}).get("title") or {}).get("content") or ""
        body = "\n".join(
            (element.get("text") or {}).get("content") or ""
            for element in card.get("elements") or []
        )
        return f"{title}\n{body}".strip()
    return ((payload.get("content") or {}).get("text") or "")


def test_vehicle_mapping() -> None:
    print("[7] 车辆 -> 项目/地区 映射（飞书标题）")
    import ap18_visualizer as viz

    cases = {
        "137657_E5-001_yanzhen_20261017": ("E5-001", "独联体地区"),
        "137501_E5-001_yanzhen_20261001": ("E5-001", "独联体地区"),
        "137250_HS5-002_yanzhen_20260922": ("HS5-002", "东南亚地区"),
        "137251_GS4-002_yanzhen_20260924": ("GS4-002", "东南亚地区"),
        "137252_LYNK01-001_yanzhen_20260928": ("LYNK01-001", "欧洲地区"),
        "137253_EX5-001_yanzhen_20261005": ("EX5-001", "南美洲地区"),
        "137254_HS5-001_yanzhen_20260929": ("HS5-001", "M57项目"),
        "137255_MG6-002_yanzhen_20260925": ("MG6-002", "M57项目"),
        "137256_R11_yanzhen_20260926": ("R11", "M57项目"),
        "random_dataset": ("", "未匹配项目/地区"),
    }
    for name, (vehicle, group) in cases.items():
        got = viz._vehicle_meta(name)
        check(got == (vehicle, group), f"{name} -> {vehicle} / {group}", str(got))
    check(
        viz._dataset_date("137657_E5-001_yanzhen_20261017") == "2026-10-17",
        "标题时间取源数据日期",
        viz._dataset_date("137657_E5-001_yanzhen_20261017"),
    )
    check(viz._dataset_date("random_dataset") == "", "无日期时标题回退到确认时间")
    check(
        viz._region_vehicles("独联体地区") == ["E5-001"],
        "地区 -> 车辆过滤",
        str(viz._region_vehicles("独联体地区")),
    )
    check(viz._region_vehicles("全部地区") == [], "全部地区不过滤")


def test_dataset_day_formats() -> None:
    print("[3] 数据日期解析（8 位 / 6 位）")
    from vehicle_regions import dataset_date, dataset_day

    check(dataset_day("137657_E5-001_yanzhen_20261017") == "20261017", "8 位日期解析")
    check(dataset_day("134277_260910") == "20260910", "6 位 YYMMDD 日期解析")
    check(dataset_day("134147_260909_two") == "20260909", "6 位日期后面带后缀也识别")
    check(dataset_day("137657") == "", "纯编号不当成日期")
    check(dataset_date("134277_260910") == "2026-09-10", "6 位日期格式化为 YYYY-MM-DD")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="verify_test_") as temp_dir:
        tmp = Path(temp_dir)
        test_probe_ready(tmp)
        test_probe_missing(tmp)
        test_resolve(tmp)
        test_frontend_wiring()
        test_dataset_day_formats()
        test_scan_parent(tmp)
        test_vehicle_mapping()
        test_http_api(tmp)
        test_confirm_and_delivery(tmp)
    print()
    if FAILED:
        print(f"结果：{FAILED} 项失败（通过 {PASSED}）")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
