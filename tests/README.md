# tests/ —— 只用于开发验证的脚本（不参与部署）

部署只用到仓库根目录的 `ap18_visualizer.py`、`parse_ap18_pcap.py`、`web/`、`run_ap18_lan.ps1`；
这个目录里的东西都是自检 / 量测 / 诊断工具，删掉也不影响程序运行。

**仓库里不带采集数据**：脚本默认的数据目录是仓库根目录的 `AP18`（历史示例数据，已被清掉），
找不到时会提示你把采集目录作为参数传进来。需要数据的脚本：`test_parse_api.py`、`test_fault_tolerance.py`、
`bench_pipeline.py`、`bench_concurrency.py`、`check_track.py`、`diagnose_pcap.py`、
`check_browser_render.js --data <目录>`；前端那两个 DOM stub 自检和 `check_syntax.js` 不需要数据。

命令都在**仓库任意目录**执行（脚本自己会把仓库根目录补进 `sys.path`，默认数据目录也按仓库根目录解析，
见 `_repo.py`）：

| 脚本 | 用途 |
| --- | --- |
| `test_parse_api.py` | 端到端 API 自检：真发 multipart 请求，校验多进程回填顺序、gzip 协商、rows 与直连解码逐字段一致（`--solo` / `--no-parallel` / `--verbose`） |
| `test_mileage.py` | 有效里程自检：现场合成 AP18 pcap（GSOF49）+ 2M/8M 设备目录，校验四类里程口径、守恒关系、错误分支与 `/api/mileage` HTTP 接口；另含惯导变种用例（累加和校验、08/09 状态码、GPS 时间基准、双日期文件名、设备时区推断）以及 SQLite 缓存/历史/扫描标注、`.pacp` 兼容、占位文件忽略、嵌套设备目录、两层日期扫描 —— **不需要任何真实采集数据** |
| `test_mileage_csv.js` | 有效里程 CSV 导出单测：单日/批量字段与数值格式、缺失标记、合计行、逗号/引号/换行转义、CRLF 与 BOM 约定 |
| `test_verify.py` | 参数验证自检：合成数据集目录预检（相机/雷达/pose/标定文件）、缺参数目录、路径解析、前端 sheet 接线、`/api/verify/*` HTTP 路由（不需要 cv2/laspy 与真实数据） |
| `test_fault_tolerance.py` | 坏文件容错自检：空文件、截断头、magic 错误、全 0 文件头、pcapng、包长越界、伪装成 pcap 的文本混在好文件里 |
| `test_frontend_faults.js` | 前端自检（DOM stub）：部分批次/文件级失败的显示、保留原视图、下载映射、失败文件列表、分批渲染、地图视角不被勾选重置、图表必画等 |
| `test_frontend_stats.js` | 前端自检：位置 RMS 超限比例的统计口径（分子=超限帧数、分母=总帧数、严格大于阈值）与页面接线 |
| `check_syntax.js` | 交付自检：用 `py_compile` / `node --check` 校验语法，并对"进程池、gzip、分批渲染、图表必画"等关键实现点做静态断言 |
| `check_browser_render.js` | 真浏览器自检：无头 Chrome + CDP 自动塞入真实文件，**读画布像素**确认每张图 / 局部轨迹真的画出来了，并检查地图视角、界面结构、页面脚本错误 |
| `bench_pipeline.py` | 解析流水线分段计时：串行 / 多进程 / multipart / JSON+gzip 各占多少，`--json` 可留档 |
| `bench_concurrency.py` | 真实 HTTP 路径计时：串行逐批 vs 并发 4 批 |
| `check_track.py` | 轨迹质量诊断：经纬度跨度、野点、跳变、GNSS/IMU 状态分布 |
| `diagnose_pcap.py` | 单文件诊断："这份数据到底能画出哪些图"——每个字段组的有效行数与 NaN 计数，并直接判定能不能画图 |
| `_repo.py` | 公共引导模块（把仓库根目录补进 `sys.path`、提供 `repo_path()`），不是测试用例 |

一次性全跑（不需要数据的三条 + 需要数据的用 `$DATA` 指定采集目录；`check_browser_render.js`
会自己起临时服务 + 无头浏览器）：

```powershell
node tests/check_syntax.js
node tests/test_frontend_faults.js
node tests/test_frontend_stats.js
python tests/test_mileage.py

$DATA = "D:\你的采集目录"
python tests/test_parse_api.py --solo "$DATA"
python tests/test_fault_tolerance.py "$DATA"
node tests/check_browser_render.js --data "$DATA" --count 28
```

注意：`check_browser_render.js` 需要一个 Chrome/Edge（默认自动找常见安装路径，可用 `--browser <exe>` 指定）。
