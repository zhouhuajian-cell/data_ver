# config.py
import os

# 路径配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(BASE_DIR, "input")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

# WGS84椭球参数 (Vincenty算法)
WGS84_A = 6378137.0  # 长半轴 m
WGS84_F = 1 / 298.257223563  # 扁率

# 轨迹分段阈值：相邻点时间差超过5分钟(300秒)切分新行程
TIME_GAP_THRESHOLD = 5 * 60  # 300秒

# 异常跳点阈值。按实际时间间隔计算允许距离，同时保留最小容差。
# 150km/h = 41.6667m/s；10ms 下约为 0.42m。
MAX_REASONABLE_SPEED_MPS = 150 / 3.6
MIN_ABNORMAL_DIST_THRESHOLD = 0.4  # 单位：米

# 兼容旧代码中使用的名字。
ABNORMAL_DIST_THRESHOLD = MIN_ABNORMAL_DIST_THRESHOLD

# GPFPD 解算状态：1/2 表示 GPS 有效，11/12 表示差分 GPS 有效。
# 0（初始化）和 3（纯惯性）不计入“GPS覆盖”里程。
GPS_STABLE_STATUS_CODES = {1, 2, 11, 12}

# 语义同 GPS_STABLE_STATUS_CODES，但用于 GGA 的定位质量字段（0-8）。
# 0 无效、6 估算、其他为各类单点/差分/RTK 定位。
GGA_STABLE_QUALITY_CODES = {1, 2, 4, 5}

# AP18 GSOF49 的 GNSS 状态码（见 parse_ap18_pcap.GNSS_STATUS）：
# 0 无定位 / 1 GNSS SPS / 2 差分 GPS / 3 GNSS PPS / 4 RTK 固定 / 5 RTK 浮动 / 6 DR 航位推算。
# 只有 1/2/3/4/5 表示卫星定位在工作；0（无解）与 6（纯惯导推算）不计入 GPS 覆盖。
# 与 GGA_STABLE_QUALITY_CODES 的语义对齐：两者都排除“无定位”和“推算”。
GSOF_STABLE_GNSS_CODES = {1, 2, 3, 4, 5}

# GPFPD 的 baseline 字段用于识别 2M/8M 数据。不同设备会有少量测量抖动，
# 所以采用目标值附近的容差，而不是要求严格等于 2.0/8.0。
BASELINE_TARGETS_M = {"2M": 2.0, "8M": 8.0}
BASELINE_TOLERANCE_M = 0.5

# GPS 周/周内秒 → Unix 秒的时间基准。
# GPFPD 的时间是「GPS 周 + 周内秒」，属于 GPS 时；直接把它当 Unix 秒用会
# 差约 10.7 年（GPS 纪元 1980-01-06 vs Unix 纪元 1970-01-01），导致和
# 设备文件名时间求交集时永远对不上。
GPS_EPOCH_UNIX_SECONDS = 315964800  # 1980-01-06 00:00:00 UTC = Unix 315964800
GPS_UTC_LEAP_SECONDS = 18           # 当前 GPS-UTC 闰秒差

# 部分设备（如俄罗斯 E5 的 rtkimu）输出的是 GPFPD 变种：
#   1) 校验和是「累加和（含 '$'）」，不是标准 NMEA 的异或；
#   2) 状态码用 08=组合导航正常、09=静止/零速修正，与 GPFPD 的 1/2/11/12 不同；
#   3) 文件名是 M57-2M_E5-001_20261003_20261003_180047_017.vin（日期出现两次）。
# 这类变种走独立状态码集合，避免影响现有 GPFPD 数据。
GPFPD_VARIANT_STABLE_STATUS_CODES = {8}

# 文件/设备级性能判定：有效里程占比达到该阈值才算“里程完整”。
# 只有存在有效里程、且2M和8M都有，才算整批数据完整。
MIN_EFFECTIVE_MILEAGE_RATIO = 0.3

# 设备文件「真的在录」的最小体积。采集失败时目录里常留下 0 字节或几字节的
# 占位文件（实测 .vin 4 B、.blf 308 B、.pack 4 B、.avi 5 KB），它们只有文件名、
# 没有内容，不能当作「这一分钟有数据」计入覆盖，否则有效里程会被算成非 0。
MIN_DEVICE_FILE_BYTES = 64 * 1024

# 设备文件名用的是哪个时区（UTC 小时数）。
# None = 与本机时区一致（默认）。实测俄罗斯 E5 的 vin_device_2M/8M：
# 「文件名时刻 + 61 秒 == 文件 mtime，而 Windows 按本机时区显示 mtime」，
# 说明设备时钟就是北京时间，和本机一致，不需要平移。
# 如果以后遇到设备时钟确实不同（例如设备固定 UTC+6），在这里填 6。
DEVICE_TIMEZONE_OFFSET_HOURS = None

# 优先逗号分隔符（适配东南亚轨迹文件）
SEPARATORS = [",", " "]

# 欧洲AP18的惯导100hz, 抽帧间隔：10ms一个点位，30000个点位间隔 5分钟
FRAME_INTERVAL_POINTS = 30000

# 文件类型标签：里程统计按这些标签归并
ROLE_2M = "2M"
ROLE_8M = "8M"
ROLE_OTHER = "其他"

# 创建文件夹
# os.makedirs(INPUT_DIR, exist_ok=True)
# os.makedirs(OUTPUT_DIR, exist_ok=True)
