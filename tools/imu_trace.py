"""Capture full-rate USB IMU traces and estimate a short, stop-to-stop stroke."""

import argparse
import csv
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median


HEADER = ("seq", "sample_us", "qw", "qx", "qy", "qz", "ax_mps2", "ay_mps2",
          "az_mps2", "gyro_norm_dps", "rest", "clipped")
RAW_HEADER = (*HEADER, "gx_raw", "gy_raw", "gz_raw")
LEGACY_FRAME_COUNT = 500
ANCHOR_SECONDS = 0.35
INTER_MOTION_QUIET_SECONDS = 0.8
WRITE_CUE_SECONDS = 1.25
HOLD_CUE_SECONDS = 3.25


@dataclass(frozen=True)
class Sample:
    seq: int
    us: int
    q: tuple
    accel: tuple
    gyro: float
    rest: bool
    clipped: bool
    gyro_raw: tuple | None = None


def parse_row(row):
    if len(row) not in (len(HEADER), len(RAW_HEADER)):
        raise ValueError("USB 样本字段数错误")
    seq, stamp = map(int, row[:2])
    values = tuple(map(float, row[2:10]))
    if not all(map(math.isfinite, values)) or not 0.98 < math.hypot(*values[:4]) < 1.02:
        raise ValueError("样本含无效四元数或运动值")
    if any(flag not in ("0", "1") for flag in row[10:12]):
        raise ValueError("静止或量程标记无效")
    gyro_raw = tuple(map(int, row[12:]))
    if any(not -32768 <= value <= 32767 for value in gyro_raw):
        raise ValueError("陀螺仪原始值超出 16 位整数范围")
    return Sample(seq, stamp, values[:4], values[4:7], values[7],
                  row[10] == "1", row[11] == "1", gyro_raw or None)


def validate(samples):
    if len(samples) < 50:
        raise ValueError("样本太少")
    if any(b.seq != a.seq + 1 for a, b in zip(samples, samples[1:])):
        raise ValueError("样本序号不连续")
    gaps = [(b.us - a.us) / 1e6 for a, b in zip(samples, samples[1:])]
    if min(gaps) <= 0 or max(gaps) > 0.040:
        raise ValueError(f"采样时间不连续，最大间隔 {max(gaps) * 1000:.1f} ms")
    if any(s.clipped for s in samples):
        raise ValueError("存在量程饱和，轨迹不可用")
    return {"samples": len(samples), "median_dt_ms": 1000 * median(gaps),
            "max_dt_ms": 1000 * max(gaps)}


def rotate(q, vector):
    w, x, y, z = q
    a, b, c = vector
    size = math.hypot(*q)
    w, x, y, z = w / size, x / size, y / size, z / size
    u, v, t = 2 * (y * c - z * b), 2 * (z * a - x * c), 2 * (x * b - y * a)
    return (a + w * u + y * t - z * v, b + w * v + z * u - x * t,
            c + w * t + x * v - y * u)


def integrate(times, vectors):
    result = [(0.0, 0.0, 0.0)]
    for i in range(1, len(times)):
        half_dt = (times[i] - times[i - 1]) / 2
        result.append(tuple(previous + (left + right) * half_dt
                            for previous, left, right in zip(result[-1], vectors[i - 1], vectors[i])))
    return result


def reconstruct(samples, tip_mm):
    times = [(s.us - samples[0].us) / 1e6 for s in samples]
    velocity = integrate(times, [s.accel for s in samples])
    corrected = [tuple(v - last * t / times[-1] for v, last in zip(vec, velocity[-1]))
                 for t, vec in zip(times, velocity)]
    imu_positions = integrate(times, corrected)
    initial_offset = rotate(samples[0].q, tip_mm)
    return [tuple(1000 * x + offset - start for x, offset, start in
                  zip(position, rotate(sample.q, tip_mm), initial_offset))
            for sample, position in zip(samples, imu_positions)]


def quiet_fraction(samples, gyro_limit=0.95, accel_limit=0.12):
    return sum(s.rest or (s.gyro < gyro_limit and math.hypot(*s.accel) < accel_limit)
               for s in samples) / len(samples)


def median_acceleration(samples):
    return tuple(median(sample.accel[axis] for sample in samples) for axis in range(3))


def smooth_median(values, radius=2):
    return [median(values[max(0, i - radius):i + radius + 1]) for i in range(len(values))]


def motion_windows(samples, *, anchor_limits=(0.95, 0.12)):
    times = [(s.us - samples[0].us) / 1e6 for s in samples]
    head = [i for i, t in enumerate(times) if t <= ANCHOR_SECONDS]
    tail = [i for i, t in enumerate(times) if t >= times[-1] - ANCHOR_SECONDS]
    for label, group in (("起点", head), ("终点", tail)):
        if quiet_fraction([samples[i] for i in group], *anchor_limits) < 0.85:
            raise ValueError(f"{label}缺少静止锚点，请保持静止后重录")
    start_bias = median_acceleration([samples[i] for i in head])
    end_bias = median_acceleration([samples[i] for i in tail])
    activity = [
        math.dist(s.accel, tuple(a + (b - a) * t / times[-1]
                                for a, b in zip(start_bias, end_bias)))
        for s, t in zip(samples, times)
    ]
    smooth = smooth_median(activity)
    smooth_gyro = smooth_median([s.gyro for s in samples])
    noise = median(smooth[i] for i in head + tail)
    threshold = max(0.08, 4 * noise)
    moving = [i for i in range(head[-1] + 1, tail[0])
              if smooth[i] > threshold or smooth_gyro[i] > 3.0]
    if not moving:
        raise ValueError(f"未找到明显动作，线加速度门限 {threshold:.3f} m/s²")
    bounds = []
    first_motion = moving[0]
    for left, right in zip(moving, moving[1:]):
        gap = times[right] - times[left]
        if gap < INTER_MOTION_QUIET_SECONDS:
            continue
        if quiet_fraction(samples[left + 1:right]) >= 0.85:
            bounds.append((first_motion, left))
            first_motion = right
        elif gap > 1.5:
            raise ValueError("动作之间缺少可用的静止锚点")
    bounds.append((first_motion, moving[-1]))
    segments = []
    for start, end in bounds:
        if times[end] - times[start] > 3.0:
            raise ValueError("动作持续时间超过 3 秒，请用一次连续的短平移")
        first = next(i for i, t in enumerate(times) if t >= times[start] - 0.10)
        last = max(i for i, t in enumerate(times) if t <= times[end] + 0.10)
        segments.append(samples[first:last + 1])
    return segments, threshold


def trajectory_summary(segment, positions):
    dx, dy, dz = positions[-1]
    initial_axis = rotate(segment[0].q, (0, 0, 1))
    axes = (rotate(s.q, (0, 0, 1)) for s in segment)
    minimum_dot = min(sum(a * b for a, b in zip(initial_axis, axis)) for axis in axes)
    tilt_change = math.degrees(math.acos(max(-1.0, min(1.0, minimum_dot))))
    return {
        "start_us": segment[0].us,
        "end_us": segment[-1].us,
        "end_xyz_mm": [round(dx, 2), round(dy, 2), round(dz, 2)],
        "endpoint_xy_mm": round(math.hypot(dx, dy), 2),
        "path_xy_mm": round(sum(math.dist(a[:2], b[:2])
                                for a, b in zip(positions, positions[1:])), 2),
        "max_xy_excursion_mm": round(max(math.hypot(x, y) for x, y, _ in positions), 2),
        "max_attitude_axis_change_deg": round(tilt_change, 2),
    }


def analyze(samples, *, static, tip_mm, expected_mm):
    report = validate(samples)
    if static:
        report["quiet_fraction"] = round(quiet_fraction(samples), 4)
        if report["quiet_fraction"] < 0.9:
            raise ValueError("静止测试中有效静止帧不足 90%")
        segment = samples
    else:
        segments, threshold = motion_windows(samples)
        if len(segments) != 1:
            raise ValueError(f"检测到 {len(segments)} 段动作，请用 --input 和 --split 分段复盘")
        segment = segments[0]
        report["motion_threshold_mps2"] = round(threshold, 4)
    positions = reconstruct(segment, tip_mm)
    report.update(trajectory_summary(segment, positions))
    if expected_mm is not None:
        report.update(expected_mm=expected_mm,
                      endpoint_error_mm=round(math.hypot(*positions[-1][:2]) - expected_mm, 2))
    return report, segment, positions


def analyze_split(samples, tip_mm):
    report = validate(samples)
    segments, threshold = motion_windows(samples)
    offset = (0.0, 0.0, 0.0)
    summaries = []
    rows = []
    start_us = segments[0][0].us
    for number, segment in enumerate(segments, 1):
        positions = reconstruct(segment, tip_mm)
        summaries.append({"segment": number, **trajectory_summary(segment, positions)})
        for sample, position in zip(segment, positions):
            absolute = tuple(a + b for a, b in zip(offset, position))
            rows.append((number, (sample.us - start_us) / 1e6, *absolute, *sample.q))
        offset = tuple(a + b for a, b in zip(offset, positions[-1]))
    report.update(
        motion_threshold_mps2=round(threshold, 4),
        segments=summaries,
        net_xyz_mm=[round(x, 2) for x in offset],
        net_xy_mm=round(math.hypot(*offset[:2]), 2),
        path_xy_mm=round(sum(s["path_xy_mm"] for s in summaries), 2),
    )
    return report, rows


def save_analysis(path, samples, *, static=False, tip_mm=(0, 0, 0), expected_mm=None, split=False):
    header = ("elapsed_s", "x_mm", "y_mm", "z_mm", "qw", "qx", "qy", "qz")
    if split:
        report, rows = analyze_split(samples, tip_mm)
        header = ("segment", *header)
    else:
        report, segment, positions = analyze(samples, static=static, tip_mm=tip_mm, expected_mm=expected_mm)
        rows = (((sample.us - segment[0].us) / 1e6, *position, *sample.q)
                for sample, position in zip(segment, positions))
    with path.with_suffix(".trajectory.csv").open("x", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    with path.with_suffix(".json").open("x", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


def capture(port, path, cue=None, *, on_progress=None, wait_for_stationary=True):
    def announce(phase, message):
        if on_progress is None:
            print(("\a" if phase in ("write", "hold") else "") + message, flush=True)
        else:
            on_progress(phase, message)

    port.reset_input_buffer()
    port.write(b"TRACE_ARM\n" if cue and wait_for_stationary else b"TRACE\n")
    deadline = time.monotonic() + 42
    started = None
    frame_count = LEGACY_FRAME_COUNT
    cue_phase = 0
    rows = []
    pending = bytearray()
    complete = False
    while not complete and time.monotonic() < deadline:
        pending.extend(port.read(port.in_waiting or 1))
        lines = pending.split(b"\n")
        pending = lines.pop()
        for line in lines:
            raw = line.decode("ascii", errors="replace").strip()
            if raw == "TRACE_ARMED":
                announce("waiting", "等待 IMU 确认静止。请先放稳笔杆。")
            elif raw == "TRACE_BEGIN" or raw.startswith("TRACE_BEGIN,"):
                if "," in raw:
                    frame_count = int(raw.split(",", 1)[1])
                started = time.monotonic()
                announce("ready", "已开始采样。请放稳笔杆，等书写提示。" if cue else
                         "已开始采样，请保持静止。")
            elif raw.startswith("TRACE_ERROR"):
                raise RuntimeError(raw)
            elif raw.startswith("CONFIG_ERROR") and started is None:
                raise RuntimeError("开发板固件不支持当前采样指令")
            elif raw.startswith("TRACE,"):
                rows.append(raw.split(",")[1:])
            elif raw == "TRACE_DONE":
                complete = True
                break
        if cue and started is not None:
            elapsed = time.monotonic() - started
            if cue_phase == 0 and elapsed >= WRITE_CUE_SECONDS:
                announce("write", f"现在移动：{cue}。")
                cue_phase = 1
            if cue_phase == 1 and elapsed >= HOLD_CUE_SECONDS:
                announce("hold", "停住，保持静止，直到结果出现。")
                cue_phase = 2
    if started is None or not complete or not rows or len(rows) != frame_count:
        raise RuntimeError(f"USB 采集未完整结束，收到 {len(rows)}/{frame_count} 帧")
    header = RAW_HEADER if len(rows[0]) == len(RAW_HEADER) else HEADER
    if any(len(row) != len(header) for row in rows):
        raise ValueError("USB 样本字段数不一致")
    samples = [parse_row(row) for row in rows]
    with path.open("x", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    print(f"原始记录：{path}", flush=True)
    return samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM10")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path, help="离线分析已有的 USB trace CSV")
    parser.add_argument("--split", action="store_true", help="离线拆开多段动作，输出各段与净位移")
    parser.add_argument("--static", action="store_true")
    parser.add_argument("--guided", action="store_true", help="依次提示沿尺子做横向、纵向平移")
    parser.add_argument("--axis", choices=("horizontal", "vertical"), help="只进行指定方向的引导测试")
    parser.add_argument("--expected-mm", type=float, default=40.0)
    parser.add_argument("--tip-mm", type=float, nargs=3, default=(0, 0, 0),
                        metavar=("X", "Y", "Z"), help="IMU 到笔尖的机体系偏移")
    args = parser.parse_args()
    if args.split and (not args.input or args.static):
        parser.error("--split 需要与 --input 一起使用")
    if not math.isfinite(args.expected_mm) or args.expected_mm <= 0:
        parser.error("--expected-mm 需要正数")
    if not all(map(math.isfinite, args.tip_mm)):
        parser.error("--tip-mm 需要三个有限数值")
    try:
        if args.input:
            with args.input.open(encoding="utf-8", newline="") as f:
                rows = csv.reader(f)
                header = tuple(next(rows, ()))
                if header not in (HEADER, RAW_HEADER):
                    raise ValueError("输入文件不是 USB trace 格式")
                samples = []
                for row in rows:
                    if len(row) != len(header):
                        raise ValueError("CSV 样本字段数与表头不一致")
                    samples.append(parse_row(row))
            save_analysis(args.output or args.input, samples, static=args.static,
                          tip_mm=args.tip_mm, expected_mm=None if args.static else args.expected_mm,
                          split=args.split)
            return 0

        import serial

        output = args.output or (
            Path(__file__).resolve().parents[1] / "recordings" / "imu-tests" /
            datetime.now().strftime("%Y%m%d-%H%M%S"))
        output.mkdir(parents=True, exist_ok=True)
        board = serial.Serial(baudrate=115200, timeout=0.08, write_timeout=2)
        board.port = args.port
        board.dtr = False
        board.rts = False
        failed = False
        with board:
            if args.guided or args.axis:
                for name, direction in (("horizontal", "横向"), ("vertical", "纵向")):
                    if args.axis and args.axis != name:
                        continue
                    cue = f"保持模块方向，沿尺子{direction}平移 {args.expected_mm:g} mm"
                    print(f"准备{direction}测试：{cue}。先把模块放稳，看到移动提示才动。", flush=True)
                    input("放稳后按回车开始：")
                    for countdown in (3, 2, 1):
                        print(f"准备 {countdown} · 保持静止", flush=True)
                        time.sleep(1)
                    path = output / f"{name}.csv"
                    try:
                        save_analysis(path, capture(board, path, cue), tip_mm=args.tip_mm,
                                      expected_mm=args.expected_mm)
                    except (RuntimeError, ValueError) as error:
                        (output / f"{name}.error.txt").write_text(str(error) + "\n", encoding="utf-8")
                        print(f"本轮未能重建：{error}", flush=True)
                        failed = True
            else:
                path = output / ("static.csv" if args.static else "stroke.csv")
                cue = None if args.static else f"平移 {args.expected_mm:g} mm"
                save_analysis(path, capture(board, path, cue), static=args.static, tip_mm=args.tip_mm,
                              expected_mm=None if args.static else args.expected_mm)
        print(f"记录目录：{output}", flush=True)
        return int(failed)
    except (OSError, ValueError, RuntimeError, EOFError) as error:
        parser.exit(1, f"测试失败：{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
