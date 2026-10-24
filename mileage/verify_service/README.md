# 里程分析服务

服务当前**只启用里程分析**一个模块。参数验证（外参/数据质检）已停用，
代码和 API 仍保留在服务里，把前端 `PARAM_ENABLED` 改回 `true` 即可恢复。

| 模块 | 状态 | API 前缀 |
| --- | --- | --- |
| **里程分析** | ✅ 启用 | `/api/mileage/*` |
| 参数验证 | ⏸ 已停用（代码保留） | `/api/param/*` |

## 启动

```bash
# 服务器（已部署）
/home/algo/miniconda3/envs/multi_traj/bin/python /home/algo/verify_service/app.py

# 或者用脚本（自带重启、后台运行、日志）
cd /home/algo/verify_service && ./start.sh
```

默认监听 `0.0.0.0:8801`。改端口：`VERIFY_PORT=9000 ./start.sh`

依赖：`fastapi uvicorn`。

## 用法

浏览器打开 `http://<host>:8801`，填数据目录，点「开始分析」。

### 两种模式

| 模式 | 输入 | 行为 |
| --- | --- | --- |
| **单日分析** | 一个数据目录 | 分析这一天，产出该目录下的 `output/mileage/.../mileage_report.html` |
| **批量刷里程** | 一个父目录 | 先扫描出所有日期目录 → **人工勾选** → 只刷选中的天 → 出汇总报表 |

批量模式先点「批量刷里程」会**只扫描不执行**，列出所有日期目录及其预检结果：

```
日期        状态     轨迹  pcap  2M文件  2M  8M文件  8M
20260915   可分析     0    67    134   有    168   有
20260916   可分析     0   420    852   有    906   有
20260917   可分析     0   455    916   有    344   有
20260918   跳过       0     0    888   有    868   有    ← 没有轨迹来源，自动置灰不可勾
```

顶部按钮：**全选 / 全不选 / 反选 / 开始刷选中的日期**，实时显示"已选 N 天"。
没有轨迹来源的目录会自动置灰、无法勾选，并写明原因。

**预检会实际抽查 pcap 内容**：有些地区的 AP18 只输出二进制 IMU 包、没有 GGA 定位语句
（比如 AT 奥地利目录），这种情况会提前判定为不可分析，不会跑完才失败：

```
AT 目录预检结果：
20260902  ✘ AP18 pcap 中不含 GGA 定位语句（只有二进制 IMU 数据），无法计算里程
20260903  ✘ AP18 pcap 中不含 GGA 定位语句（只有二进制 IMU 数据），无法计算里程
...
```

这类日期在批量结果里记为**跳过**（不是失败），原因写在备注里。0 字节的占位文件
（如「数据验证不可用.txt」）也会被忽略，不会被当成轨迹。

会给更上层的目录也可以，例如：

```
\\192.168.20.206\logs_new\018_LYNK01-001\overseas\Europe\RO
└── 20260915 / 20260916 / 20260917 / 20260918   ← 自动识别这些日期目录
```

批量结果写在父目录的 `output/batch/task_<时间>/` 下：
- `batch_summary.html` —— 汇总报表（每天一行 + 合计）
- `batch_summary.csv` —— 同样内容的表格数据

找不到轨迹的日子会自动跳过并在报表里写明原因，不影响其他天。

### 路径写法（重点）

服务跑在服务器 Linux 上，**Windows 共享路径会自动映射**，两种写法都能用：

| 你输入 | 服务实际使用 |
| --- | --- |
| `\\192.168.20.206\logs_new\...\20260916` | `/mnt/logs_new/.../20260916` |
| `/mnt/logs_new/.../20260916` | 原样使用 |

映射规则是**读取服务器真实的 CIFS 挂载表**（`/proc/mounts`）动态生成的，
不是写死的。当前服务器上的映射：

```
\\192.168.20.206\logs_new        -> /mnt/logs_new
\\192.168.20.206\Data_Platform   -> /mnt/Data_Platform
\\192.168.20.94\share            -> /mnt/share
\\192.168.20.94\MaxieyeRawData   -> /mnt/MaxieyeRawData
\\192.168.20.87\done_data        -> /mnt/done_data
\\192.168.20.93\logs             -> /mnt/logs
```

以后运维新增挂载，服务不用改代码也能自动识别。页面探测成功后会把输入框里的路径
回显成实际使用的 Linux 路径，方便确认。

### 数据目录要求

数据目录里放轨迹来源 + 2M/8M 数据：

```
<数据目录>/
├── trajectory_xxx.txt                  # $GPFPD / $GPGGA / CSV
├── M57_2M_LYNK01_001_0917_1_20260917_142635/   # 2M 数据
└── M57_8M_LYNK01_001_0917_1_20260917_142635/   # 8M 数据
```

也支持 **AP18 惯导 pcap**：数据目录下有 `AP18/*.pcap` 或顶层有 `*.pcap` 时
（没有 txt/csv 轨迹），服务会解析 pcap 里的 `$GNGGA`/`$INGGA` 语句自动还原轨迹，
再算里程。420 个 pcap（530 MB）约 12–20 秒解析完。

### 输出

- **里程**：全部轨迹长度、GPS 稳定里程、**实际有效里程**
- **覆盖**：2M/8M 各自的时间起止、时长、文件数、容量

**里程口径**：

| 指标 | 含义 |
| --- | --- |
| 全部轨迹长度 | 轨迹所有相邻点距离之和 |
| GPS 稳定里程 | 相邻两点定位质量都稳定（1/2/4/5）且非跳点的距离之和 |
| **实际有效里程** | 该段 GPS 稳定 **且** 当时 2M 或 8M 相机正在录制 |

> **2M/8M 是传感器，本身没有「里程」概念**，只提供录制时段。
> 设备文件按「组起始时刻 + 组序号×60 秒」推算，一个文件代表约 60 秒有数据。
> 轨迹某一段的时间中点落在 2M 或 8M 的任一段时间区间内，就算「当时在录」。

`output` 目录在探测阶段会被识别并排除，不会被当成数据源重复读取。

## 输出位置

结果默认写回**被分析的数据目录**下的 `output/`，跟着数据走：

```
<数据目录>/output/
└── mileage/task_<时间>_<随机>/mileage_report.html   # 单日报告（只输出这一份 HTML）
```

批量模式在父目录下：

```
<父目录>/output/batch/task_<时间>_<随机>/
├── batch_summary.html   # 汇总报表
└── batch_summary.csv    # 表格数据
```

想改写到别处时，请求体里传 `output_root` 即可。

## API

```bash
curl -X POST http://HOST:8801/api/mileage/probe    -d '{"data_root":"/path"}' -H "Content-Type: application/json"
curl -X POST http://HOST:8801/api/mileage/analyze  -d '{"data_root":"/path"}' -H "Content-Type: application/json"
curl http://HOST:8801/api/mileage/tasks/{id}/progress
curl http://HOST:8801/api/mileage/tasks/{id}
curl "http://HOST:8801/api/mileage/tasks/{id}/download?name=mileage_summary.csv"
```

结果页可分享：`/mileage/report/{task_id}`

### 批量

```bash
# 1. 先扫描（只列目录 + 预检，不执行）
curl -X POST http://HOST:8801/api/batch/discover -d '{"root":"/path/to/parent"}' -H "Content-Type: application/json"

# 2. 分析全部
curl -X POST http://HOST:8801/api/batch/analyze  -d '{"root":"/path/to/parent"}' -H "Content-Type: application/json"

# 3. 只分析指定日期（人工挑选）
curl -X POST http://HOST:8801/api/batch/analyze \
  -d '{"root":"/path/to/parent","selected":["20260916","20260917"]}' \
  -H "Content-Type: application/json"

curl http://HOST:8801/api/batch/tasks/{id}/progress
curl http://HOST:8801/api/batch/tasks/{id}
curl "http://HOST:8801/api/batch/tasks/{id}/download?name=batch_summary.html"
```

`selected` 里有不存在的日期会返回 400 并列出可用日期。

汇总报表页：`/batch/report/{task_id}`

## 文件

- `app.py` —— FastAPI 路由
- `mileage.py` —— **里程分析**：数据探测、覆盖统计、里程计算、任务队列
- `pcap_gga.py` —— AP18 pcap → GGA 轨迹解析
- `mileage_core/` —— 里程计算器（trajectory_parser / distance_calc / config / utils）
- `verifier.py` —— 参数验证（已停用，代码保留）
- `static/index.html` —— 前端
- `start.sh` / `start.bat` —— 启动脚本


## 启动

```bash
# 服务器（已部署）
/home/algo/miniconda3/envs/multi_traj/bin/python /home/algo/verify_service/app.py

# 或者用脚本（自带重启、后台运行、日志）
cd /home/algo/verify_service && ./start.sh
```

默认监听 `0.0.0.0:8801`。改端口：`VERIFY_PORT=9000 ./start.sh`

依赖：`fastapi uvicorn`，参数验证另需 `opencv-python laspy numpy pandas pyyaml scipy openpyxl matplotlib`。

## 用法

浏览器打开 `http://<host>:8801`，上面两个标签：

- **参数验证**：填含 `parameter_yaml` / `cam_*` / `lidar_*` / pose 的目录 → 点「开始验证」
- **里程分析**：填含轨迹文件 + 2M/8M 数据的目录 → 点「开始分析」

### 参数验证

数据目录要求（目录名由 topic 推导，`/cam/front` → `cam_front`）：

```
<数据目录>/
├── parameter_yaml/<标定日期>/
│   ├── sensors_used.yaml      # nickname: "camera-1, /cam/front"
│   ├── topic_map.yaml
│   ├── intrinsic/camera-1.yaml
│   └── extrinsic/lidar-1_to_camera-1.yaml
├── cam_front/                  *.jpg
├── lidar_top/                  *.las
└── rtkimu_qj_7683/Vehicle(...).pose
```

必须存在昵称为 `front` / `front_wide` / `front_narrow` 的相机
（FrameMap 生成以前视相机为基准）。

结论：`外参验证通过 N/N`、`外参验证不通过 M/N`（列出具体哪些相机缺什么）。

### 里程分析

数据目录里放轨迹文件（`$GPFPD` / `$GPGGA` 的 `.txt`，或带状态列的 `.csv`），
以及 2M/8M 数据目录（名字里带 `2M` / `8M` 即可识别）：

```
<数据目录>/
├── trajectory_xxx.txt                  # $GPFPD / $GPGGA / CSV
├── M57_2M_LYNK01_001_0917_1_20260917_142635/   # 2M 数据
└── M57_8M_LYNK01_001_0917_1_20260917_142635/   # 8M 数据
```

也支持 **AP18 惯导 pcap**：数据目录下有 `AP18/*.pcap` 时（没有 txt/csv 轨迹），
服务会解析 pcap 里的 `$GNGGA`/`$INGGA` 语句自动还原轨迹，再算里程。
420 个 pcap（530 MB）约 12–20 秒解析完。

输出：

- **里程**：GPS稳定里程、2M 里程、8M 里程、**实际有效里程**（GPS稳定 ∩ 2M/8M）
- **覆盖**：2M/8M 各自的时间起止、时长、文件数、容量；缺任一类即标记异常

设备文件时间按「组起始时刻 + 组序号×60 秒」推算，所以同一组内的文件能体现时间推进。

> **AP18 轨迹的说明**：GGA 语句不含 2M/8M 基线字段，所以 2M/8M 里程会是 0。
> 这种情况以 **GPS 稳定里程**作为有效里程，判定为通过而不是失败。

## 输出位置

结果默认写回**被分析的数据目录**下的 `output/`，跟着数据走：

```
<数据目录>/output/
├── param/task_<时间>_<随机>/     # 参数验证：Excel、图表、sensor_status.json
└── mileage/task_<时间>_<随机>/   # 里程分析：mileage_summary.csv、mileage_segments.csv
```

`output` 目录在探测阶段会被识别并排除，不会被当成数据源重复读取。

想改写到别处时，请求体里传 `output_root` 即可。


## API

```bash
# 参数验证
curl -X POST http://HOST:8801/api/param/probe      -d '{"data_root":"/path"}' -H "Content-Type: application/json"
curl -X POST http://HOST:8801/api/param/verify     -d '{"data_root":"/path"}' -H "Content-Type: application/json"
curl http://HOST:8801/api/param/tasks/{id}/progress
curl http://HOST:8801/api/param/tasks/{id}

# 里程分析
curl -X POST http://HOST:8801/api/mileage/probe    -d '{"data_root":"/path"}' -H "Content-Type: application/json"
curl -X POST http://HOST:8801/api/mileage/analyze  -d '{"data_root":"/path"}' -H "Content-Type: application/json"
curl http://HOST:8801/api/mileage/tasks/{id}/progress
curl http://HOST:8801/api/mileage/tasks/{id}

# 下载
curl "http://HOST:8801/api/mileage/tasks/{id}/download?name=mileage_summary.csv"
```

结果页可直接分享：

- 参数验证：`/report/{task_id}`
- 里程分析：`/mileage/report/{task_id}`

## 服务做了哪些兼容处理

原参数验证脚本是给 Linux 环境写的，直接跑会遇到几个问题，服务里做了处理（**不改动原脚本**）：

1. **全局变量注入**：两个入口函数只读模块级 `DATA_ROOT` / `POSE_FILE` 等，不接受参数。
   服务在每次任务前临时覆写、跑完还原，并用锁串行执行。
2. **外参 ID 自动修正**：原脚本默认 `LIDAR_EXTRINSIC_ID_OVERRIDE="body-2"`，
   若数据集外参实际是 `lidar-1_to_camera-1.yaml`，会被误判成 `missing_extrinsic`。
   服务检测目录里实际存在的 `*_to_*.yaml` 并自动切到正确 ID。
3. **预处理外参路径**：原脚本 `ALIGN_PRE_TRANSFORM_FILE` 指向硬编码 Linux 路径，改为自动查找。
4. **matplotlib 兼容补丁**：`Axes.boxplot(labels=)` 在 matplotlib ≥ 3.9 改名 `tick_labels`。
5. **输出隔离**：每个任务独立输出目录，避免历史产物混进本次结果。

## 文件

- `app.py` —— FastAPI 路由（两个模块的 API 分开）
- `verifier.py` —— **参数验证**：目录探测、全局变量注入、兼容补丁、任务队列
- `mileage.py` —— **里程分析**：数据探测、覆盖统计、里程计算、任务队列
- `static/index.html` —— 前端（双标签）
- `start.sh` / `start.bat` —— 启动脚本
- `output/param/`、`output/mileage/` —— 两个模块各自的产出目录

