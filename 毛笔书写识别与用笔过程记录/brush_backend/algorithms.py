import math

import numpy as np
from scipy.signal import butter, sosfiltfilt

CHANNELS = ("ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps")
LABELS = ("一", "十", "口")
VERSION = "six-axis-dtw/1.0"
PREPROCESS = {"rate_hz": 50, "max_points": 200, "band_fraction": 0.25}


def inspect(samples: list[dict]):
    issues, warnings = [], []
    result = {"valid": False, "count": len(samples), "issues": issues, "warnings": warnings}
    if len(samples) < 2:
        issues.append("至少需要两个采样点。")
        return result
    times = np.array([s["sample_us"] for s in samples], dtype=float) / 1e6
    delta = np.diff(times)
    span = float(times[-1] - times[0])
    rate = (len(samples) - 1) / span if span > 0 else 0.0
    result.update(duration_s=max(span, 0), saved_hz=rate, median_interval_ms=float(np.median(delta) * 1000),
                  p95_interval_ms=float(np.percentile(delta, 95) * 1000), max_gap_ms=float(np.max(delta) * 1000),
                  long_gaps=int(np.count_nonzero(delta > 0.100001)))
    if np.any(delta <= 0):
        issues.append("设备时间戳存在重复或倒序。")
    if span < 0.5 or span > 15:
        issues.append("单字识别支持 0.5～15 秒；该文件仍可查看和回放。")
    if rate < 45:
        issues.append("实际保存频率低于 45 Hz，保留报告，暂不用于模板或识别。")
    if np.any(delta > 0.100001):
        issues.append("设备采样时间存在超过 100 ms 的缺口。")
    boots = {str(s["boot_id"]) for s in samples if s.get("boot_id") is not None}
    if len(boots) > 1:
        issues.append("一条记录包含不同 boot_id，可能发生设备重启。")
    if all(s.get("seq") is not None for s in samples):
        step = np.diff([s["seq"] for s in samples])
        result["missing_sequences"] = int(np.sum(np.maximum(step - 1, 0)))
        if np.any(step != 1):
            issues.append("采样序号存在缺号、重复或倒序；先核对序号是否按采样递增。")
    else:
        warnings.append("序号不完整，无法验证采样序号连续性。")
    if any(s.get("clipped") in (1, True, "1") for s in samples):
        issues.append("记录出现 clipped 标记，请先核对传感器量程。")
    result["valid"] = not issues
    return result


def prepare(samples: list[dict]):
    time = np.array([s["sample_us"] for s in samples], dtype=float) / 1e6
    time -= time[0]
    values = np.array([[s[c] for c in CHANNELS] for s in samples], dtype=float)
    # 先插值到近似源采样网格，低通，再降采样；长缺口已在质量检查中拒绝。
    source_hz = min(1000.0, 1 / np.median(np.diff(time)))
    source_grid = np.linspace(0, time[-1], max(4, int(time[-1] * source_hz) + 1))
    uniform = np.column_stack([np.interp(source_grid, time, values[:, i]) for i in range(6)])
    if len(uniform) > 18:
        uniform = sosfiltfilt(butter(2, min(15.0, source_hz * 0.35), fs=source_hz, output="sos"), uniform, axis=0)
    count = min(PREPROCESS["max_points"], max(4, int(time[-1] * PREPROCESS["rate_hz"]) + 1))
    target = np.linspace(0, time[-1], count)
    return np.column_stack([np.interp(target, source_grid, uniform[:, i]) for i in range(6)])


def normalizer(arrays):
    matrix = np.concatenate(arrays)
    return matrix.mean(axis=0), np.maximum(matrix.std(axis=0), [0.02, 0.02, 0.02, 0.5, 0.5, 0.5])


def summary_features(values):
    return np.concatenate([values.mean(axis=0), values.std(axis=0),
                           np.sqrt(np.mean(values ** 2, axis=0)), np.ptp(values, axis=0)])


def dtw(a, b):
    """窗口约束多维 DTW；返回沿最优路径的局部代价 RMS。"""
    n, m = len(a), len(b)
    width = max(abs(n - m), int(max(n, m) * PREPROCESS["band_fraction"]))
    previous = np.full(m + 1, np.inf)
    previous[0] = 0
    previous_len = np.zeros(m + 1, dtype=int)
    for i in range(1, n + 1):
        current = np.full(m + 1, np.inf)
        current_len = np.zeros(m + 1, dtype=int)
        lo, hi = max(1, i - width), min(m, i + width)
        costs = np.sum((b[lo - 1:hi] - a[i - 1]) ** 2, axis=1)
        for j in range(lo, hi + 1):
            choices = (previous[j], current[j - 1], previous[j - 1])
            k = min(range(3), key=choices.__getitem__)
            length = (previous_len[j], current_len[j - 1], previous_len[j - 1])[k]
            current[j] = costs[j - lo] + choices[k]
            current_len[j] = length + 1
        previous, previous_len = current, current_len
    if not math.isfinite(float(previous[m])):
        raise ValueError("DTW 路径不可用。")
    return float(np.sqrt(previous[m] / max(1, previous_len[m])))


def metrics(samples):
    if not samples:
        return {"peak": None, "angle_ranges": {}, "distance": None}
    speed = [math.sqrt(sum(float(s[c]) ** 2 for c in CHANNELS[3:])) for s in samples]
    index = int(np.argmax(speed))
    peak = {"value_dps": speed[index], "sample_index": index, "seq": samples[index].get("seq"),
            "at_s": (samples[index]["sample_us"] - samples[0]["sample_us"]) / 1e6}
    angles = {}
    for name in ("roll_deg", "pitch_deg", "yaw_deg"):
        if all(s.get(name) is not None for s in samples):
            series = np.degrees(np.unwrap(np.radians([s[name] for s in samples])))
            angles[name] = {"min": float(series.min()), "max": float(series.max()), "range": float(np.ptp(series))}
    distances = [s["distance_mm"] for s in samples if s.get("distance_mm") is not None]
    distance = {"last_mm": distances[-1], "min_mm": min(distances), "max_mm": max(distances),
                "source": "硬件算法估计，尚未验证为真实笔尖位移"} if distances else None
    return {"peak": peak, "angle_ranges": angles, "distance": distance}
