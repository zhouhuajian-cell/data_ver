# distance_calc.py
import math
from config import WGS84_A, WGS84_F

def deg2rad(deg: float) -> float:
    """角度转弧度"""
    return deg * math.pi / 180.0

def haversine_2d(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Haversine球面快速距离，单位米"""
    lat1_r = deg2rad(lat1)
    lon1_r = deg2rad(lon1)
    lat2_r = deg2rad(lat2)
    lon2_r = deg2rad(lon2)

    d_lat = lat2_r - lat1_r
    d_lon = lon2_r - lon1_r

    a = math.sin(d_lat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(d_lon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return WGS84_A * c

def vincenty_2d(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Vincenty高精度椭球距离（推荐GPS轨迹），单位米"""
    L = deg2rad(lon2 - lon1)
    U1 = math.atan((1 - WGS84_F) * math.tan(deg2rad(lat1)))
    U2 = math.atan((1 - WGS84_F) * math.tan(deg2rad(lat2)))
    sinU1, cosU1 = math.sin(U1), math.cos(U1)
    sinU2, cosU2 = math.sin(U2), math.cos(U2)

    lam = L
    for _ in range(100):
        sinLam = math.sin(lam)
        cosLam = math.cos(lam)
        sinSigma = math.sqrt((cosU2 * sinLam) ** 2 + (cosU1 * sinU2 - sinU1 * cosU2 * cosLam) ** 2)
        if sinSigma == 0:
            return 0.0
        cosSigma = sinU1 * sinU2 + cosU1 * cosU2 * cosLam
        sigma = math.atan2(sinSigma, cosSigma)
        sinAlpha = cosU1 * cosU2 * sinLam / sinSigma
        cosSqAlpha = 1 - sinAlpha ** 2
        C = WGS84_F / 16 * cosSqAlpha * (4 + WGS84_F * (4 - 3 * cosSqAlpha)) if cosSqAlpha != 0 else 0
        lam_prev = lam
        lam = L + (1 - C) * WGS84_F * sinAlpha * (
                sigma + C * sinSigma * (2 * math.cos(2 * sigma) + C * math.cos(sigma) * (-1 + 2 * math.cos(2 * sigma) ** 2))
        )
        if abs(lam - lam_prev) < 1e-12:
            break
    uSq = cosSqAlpha * (WGS84_A ** 2 - (WGS84_A * (1 - WGS84_F)) ** 2) / (WGS84_A * (1 - WGS84_F)) ** 2
    A = 1 + uSq / 16384 * (4096 + uSq * (-768 + uSq * (320 - 175 * uSq)))
    B = uSq / 1024 * (256 + uSq * (-128 + uSq * (74 - 47 * uSq)))
    # 修复点：math.sinSigma → sinSigma
    deltaSigma = B * sinSigma * (
            math.cos(2 * sigma) + B / 4 * ((2 * math.cos(4 * sigma) - 1) * math.cos(2 * sigma))
            - B / 6 * math.cos(4 * sigma) * (-3 + 4 * sinSigma ** 2) * (-3 + 4 * math.cos(2 * sigma) ** 2)
    )
    s = WGS84_A * (1 - WGS84_F) * A * (sigma - deltaSigma)
    return s

def calc_3d_distance(lon1, lat1, alt1, lon2, lat2, alt2, use_vincenty: bool = True) -> float:
    """计算含高程的三维空间直线距离，单位米"""
    if use_vincenty:
        ground_dist = vincenty_2d(lon1, lat1, lon2, lat2)
    else:
        ground_dist = haversine_2d(lon1, lat1, lon2, lat2)
    alt_diff = alt2 - alt1
    space_dist = math.sqrt(ground_dist ** 2 + alt_diff ** 2)
    return space_dist
