"""逐笔 USB 采集、轨迹重建与形状评分。

复用 tools/imu_trace.py 的 USB TRACE 采集/重建，复用 tools/ink_coach.py 的
动笔锚点检测与分段，把腕墨练习站的能力并入墨序工作台。
"""
from __future__ import annotations

import csv
import json
import math
import sys
import threading
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .schemas import RecordInfo
from .service import AppError

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT.parent / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import imu_trace  # noqa: E402
import ink_coach  # noqa: E402

GLYPHS = ink_coach.GLYPHS
GUIDES = {
    "三": [
        [[91, 91], [231, 87]],
        [[107, 151], [215, 149]],
        [[68, 222], [251, 219]],
    ],
    "十": [
        [[65, 136], [251, 132]],
        [[163, 62], [159, 258]],
    ],
    "口": [
        [[90, 91], [94, 227]],
        [[90, 92], [228, 88], [218, 227]],
        [[94, 225], [220, 221]],
    ],
}
TYPES = {
    "三": ("horizontal", "horizontal", "horizontal"),
    "十": ("horizontal", "vertical"),
    "口": ("vertical", "bend", "horizontal"),
}
SAMPLE_FILE = TOOLS / "ink_web" / "sample.json"
ACTIVE_PHASES = ("opening", "waiting", "ready", "write", "hold", "analyzing")


def _distances(points):
    offsets = [0.0]
    for previous, current in zip(points, points[1:]):
        offsets.append(offsets[-1] + math.hypot(current[0] - previous[0],
                                                current[1] - previous[1]))
    return offsets


def _resample(points, offsets, count=32):
    last = offsets[-1]
    index = 1
    result = []
    for k in range(count):
        distance = last * k / (count - 1)
        while index < len(offsets) - 1 and offsets[index] < distance:
            index += 1
        span = offsets[index] - offsets[index - 1]
        fraction = 0.0 if not span else (distance - offsets[index - 1]) / span
        result.append([points[index - 1][axis] +
                       (points[index][axis] - points[index - 1][axis]) * fraction
                       for axis in (0, 1)])
    return result


def _fit_path(points, target):
    lengths = _distances(points)
    target_lengths = _distances(target)
    target_length = target_lengths[-1]
    if lengths[-1] < 0.001 or target_length < 0.001:
        return None
    reference = _resample(target, target_lengths)
    sample = _resample(points, lengths)
    dot = cross = 0.0
    for point, expected in zip(sample, reference):
        ax, ay = point[0] - sample[0][0], point[1] - sample[0][1]
        bx, by = expected[0] - reference[0][0], expected[1] - reference[0][1]
        dot += ax * bx + ay * by
        cross += ax * by - ay * bx
    angle = math.atan2(cross, dot)
    ratio = target_length / lengths[-1]
    cos, sin = math.cos(angle), math.sin(angle)

    def align(point):
        x, y = point[0] - points[0][0], point[1] - points[0][1]
        return [target[0][0] + ratio * (cos * x - sin * y),
                target[0][1] + ratio * (sin * x + cos * y)]

    aligned = [align(point) for point in sample]
    errors = [math.hypot(point[0] - expected[0], point[1] - expected[1])
              for point, expected in zip(aligned, reference)]
    rms = math.sqrt(sum(error * error for error in errors) / len(errors))
    return {"draw": [align(point) for point in points],
            "score": max(0, round(100 * (1 - 3.2 * rms / target_length))),
            "peak": errors.index(max(errors)) / 31, "rms": rms}


def _measure(points):
    return [[point[1], -point[2]] for point in points]


def _valid_points(points, limit=5000):
    if not isinstance(points, list) or not 1 <= len(points) <= limit:
        raise AppError("轨迹点数量不正确。", 422)
    for point in points:
        if (not isinstance(point, list) or len(point) < 3 or
                any(type(value) not in (int, float) or not math.isfinite(value)
                    for value in point[:3])):
            raise AppError("轨迹点格式不正确。", 422)


def _tilt_difference(current, reference):
    def at_phase(record, phase):
        duration = float(record.get("duration_s") or 0)
        target = phase * duration
        for point in record.get("points") or []:
            if point[0] >= target:
                return point[3]
        return (record.get("points") or [[0, 0, 0, 0]])[-1][3]

    return max(abs(at_phase(current, index / 20) - at_phase(reference, index / 20))
               for index in range(21))


def review_points(record, glyph, stroke, reference=None):
    """把实测轨迹与范线（或个人示范）对齐，返回形状接近度与反馈。"""
    if glyph not in GUIDES or not 0 <= stroke < len(GUIDES[glyph]):
        raise AppError("选字或笔画无效。", 422)
    points = record.get("points") or []
    _valid_points(points)
    guide = GUIDES[glyph][stroke]
    measured = _measure(points)

    teacher = None
    if reference:
        _valid_points(reference.get("points") or [])
        teacher = _fit_path(_measure(reference["points"]), guide)
    target = teacher["draw"] if teacher else guide
    fit = _fit_path(measured, target)
    if fit is None:
        raise AppError("轨迹长度不足，无法与范线对齐。", 422)

    peak = fit["peak"]
    where = "起笔段" if peak < .33 else "中段" if peak < .67 else "收笔段"
    if fit["score"] >= 90:
        feedback = "轮廓贴近参考，可以尝试保持同样的运笔节奏。"
    else:
        feedback = f"{where}与参考线的偏差最明显。回放该段，下一遍尽量沿范线稳定推进。"
    if reference:
        difference = float(record.get("duration_s") or 0) - float(reference.get("duration_s") or 0)
        feedback += f" 用时较示范{'短' if difference < 0 else '长'} {abs(difference):.2f} 秒。"
        if record.get("axis_label") and record.get("axis_label") == reference.get("axis_label"):
            feedback += f" 倾角最大差 {_tilt_difference(record, reference):.1f}°。"
    segments = int(record.get("segment_count") or 1)
    if segments > 1:
        feedback += f" 共检测到 {segments} 段，本页采用主段。"

    return {"score": fit["score"], "peak": fit["peak"], "rms": round(fit["rms"], 3),
            "against": "reference" if reference else "guide",
            "draw": fit["draw"], "reference_draw": teacher["draw"] if teacher else None,
            "guide": guide, "feedback": feedback,
            "note": "形状接近度来自轨迹对齐，只作为练习参考，不是书法或字形评级。"}


def parse_tip(value):
    try:
        parts = [float(part) for part in str(value).split(",")]
    except ValueError:
        raise ValueError("BRUSH_TIP_MM 需要三个数值")
    if len(parts) != 3 or not all(map(math.isfinite, parts)):
        raise ValueError("BRUSH_TIP_MM 需要三个有限数值")
    return tuple(parts)


def _euler(q):
    w, x, y, z = q
    size = math.hypot(w, x, y, z) or 1.0
    w, x, y, z = w / size, x / size, y / size, z / size
    roll = math.degrees(math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x)))))
    yaw = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
    return roll, pitch, yaw


def brush_samples(segment):
    """把 USB TRACE 段转成墨序的六轴样本。

    TRACE 的加速度是世界系线性加速度；用四元数逆变换回机体系并叠加 +g，
    得到与 MPU6050 一致的比力。三轴陀螺优先使用 16 位原始值 / 65.5。
    """
    gravity = (0.0, 0.0, 9.80665)
    rows = []
    for sample in segment:
        inverse = (sample.q[0], -sample.q[1], -sample.q[2], -sample.q[3])
        linear = imu_trace.rotate(inverse, sample.accel)
        body_gravity = imu_trace.rotate(inverse, gravity)
        accel = [(linear[axis] + body_gravity[axis]) / 9.80665 for axis in range(3)]
        if sample.gyro_raw:
            gyro = [value / 65.5 for value in sample.gyro_raw]
        else:
            gyro = [0.0, 0.0, sample.gyro]
        roll, pitch, yaw = _euler(sample.q)
        row = {"sample_us": int(sample.us), "seq": int(sample.seq),
               "ax_g": accel[0], "ay_g": accel[1], "az_g": accel[2],
               "gx_dps": gyro[0], "gy_dps": gyro[1], "gz_dps": gyro[2],
               "roll_deg": roll, "pitch_deg": pitch, "yaw_deg": yaw,
               "rest": int(sample.rest), "clipped": int(sample.clipped),
               "boot_id": "usb-trace"}
        if not sample.gyro_raw:
            row["gyro_fallback"] = True
        rows.append(row)
    return rows


class StrokeStation:
    def __init__(self, port="COM10", tip_mm=(0, 0, 0), directory=None,
                 board_factory=None):
        self.port = port
        self.tip_mm = tuple(tip_mm)
        self.directory = Path(directory or (ROOT.parent / "recordings" / "ink-coach"))
        self.board_factory = board_factory or ink_coach.open_board
        self.lock = threading.Lock()
        self.jobs = deque(maxlen=16)

    def _find(self, job_id):
        for job in reversed(self.jobs):
            if job["id"] == job_id:
                return job
        return None

    def status(self, job_id=None):
        with self.lock:
            for job in reversed(self.jobs):
                if job_id is None or job["id"] == job_id:
                    return {key: value for key, value in job.items() if key != "segment"}
            return None

    def _update(self, job_id, phase, message, **fields):
        with self.lock:
            job = self._find(job_id)
            if job is not None:
                job.update(phase=phase, message=message, **fields)

    def start(self, glyph, stroke):
        if glyph not in GLYPHS or type(stroke) is not int or not 0 <= stroke < len(GLYPHS[glyph]):
            raise AppError("选字或笔画无效。", 422)
        with self.lock:
            latest = self.jobs[-1] if self.jobs else None
            if latest and latest["phase"] in ACTIVE_PHASES:
                return None
            job = {"id": uuid.uuid4().hex[:12], "glyph": glyph, "stroke": stroke,
                   "phase": "opening", "message": "正在连接 USB…"}
            self.jobs.append(job)
        threading.Thread(target=self._record, args=(job["id"], glyph, stroke),
                         daemon=True).start()
        return job["id"]

    def _record(self, job_id, glyph, stroke):
        key = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + job_id
        folder = (self.directory / glyph / f"{stroke + 1:02d}-{GLYPHS[glyph][stroke]}" / key)
        raw_path = folder / "imu.csv"
        try:
            folder.mkdir(parents=True)
            pending = folder / "imu.part"
            with self.board_factory(self.port) as board:
                samples = imu_trace.capture(board, pending, cue=f"书写{GLYPHS[glyph][stroke]}",
                                            on_progress=lambda phase, message:
                                            self._update(job_id, phase, message),
                                            wait_for_stationary=False)
            pending.rename(raw_path)
            self._update(job_id, "analyzing", "正在重建轨迹…")
            report = imu_trace.validate(samples)
            segment, threshold, segment_count = ink_coach.guided_stroke(samples)
            positions = imu_trace.reconstruct(segment, self.tip_mm)
            if math.hypot(*positions[-1][:2]) < 2:
                raise ValueError("检测到的平面位移太小，请放大字幅后重录")
            report.update(imu_trace.trajectory_summary(segment, positions),
                          motion_threshold_mps2=round(threshold, 4))
            tip_norm = math.hypot(*self.tip_mm)
            axis = tuple(value / tip_norm for value in self.tip_mm) if tip_norm else (0, 0, 1)
            result = ink_coach.pack_result(
                segment, positions, report, axis,
                "笔尖偏移轴" if tip_norm else "IMU Z 轴", "USB 实板", segment_count)
            result.update(glyph=glyph, stroke=stroke, recording=key)
            result["review"] = review_points(result, glyph, stroke)
            with (folder / "stroke.json").open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False)
            with self.lock:
                job = self._find(job_id)
                if job is not None:
                    job.update(phase="done", message="本笔已保存，可以回放。",
                               result=result, segment=segment, recording=key)
        except (OSError, RuntimeError, ValueError, ImportError) as error:
            message = str(error)
            if message == "TRACE_ERROR IMU not ready or trace busy":
                message = "IMU 尚未就绪或正在采样。请等板载屏幕显示姿态后重试。"
            if raw_path.is_file():
                message += " 本笔的采样 CSV 已保存。"
            self._update(job_id, "error", message)

    def sample(self, direction):
        if direction not in ("horizontal", "vertical", "bend"):
            raise AppError("示例方向无效。", 404)
        if direction == "bend":
            raise AppError("横折暂无实测示例，请用实板录制。", 404)
        try:
            data = json.loads(SAMPLE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise AppError("示例轨迹文件不可用。", 404)
        if direction not in data:
            raise AppError("示例轨迹文件缺少该方向。", 404)
        return {"demo": direction, **data[direction]}

    def meta(self):
        with self.lock:
            latest = next((job for job in reversed(self.jobs)), None)
            busy = bool(latest and latest["phase"] in ACTIVE_PHASES)
            current = {key: value for key, value in latest.items() if key != "segment"} \
                if latest else None
        return {
            "port": self.port,
            "tip_mm": list(self.tip_mm),
            "busy": busy,
            "job": current,
            "glyphs": [{"name": name,
                        "strokes": [{"index": index, "name": label,
                                     "type": TYPES[name][index],
                                     "guide": GUIDES[name][index]}
                                    for index, label in enumerate(labels)]}
                       for name, labels in GLYPHS.items()],
        }

    def save(self, service, job_id, writer_id, mount_id):
        with self.lock:
            job = self._find(job_id)
            if job is None:
                raise AppError("找不到本次采集。", 404)
            if job["phase"] != "done" or not job.get("segment"):
                raise AppError("本笔还没有完成采集。", 409)
            segment = job["segment"]
            result = job["result"]
            recording = job.get("recording")
        samples = brush_samples(segment)
        if not samples:
            raise AppError("本笔没有可保存的样本。", 409)
        info = RecordInfo(writer_id=writer_id, mount_id=mount_id, mode="practice",
                          label=None,
                          title=f"USB 逐笔 · {result['glyph']} · "
                                f"{GLYPHS[result['glyph']][result['stroke']]} · "
                                f"{datetime.now().strftime('%H:%M:%S')}")
        record = service.create(info, samples, source="real",
                                filename=f"usb-stroke-{recording}.csv")
        record.update(input_kind="usb-trace-stroke",
                      stroke={"glyph": result["glyph"], "index": result["stroke"],
                              "name": GLYPHS[result["glyph"]][result["stroke"]],
                              "recording": recording},
                      stroke_review=result.get("review"))
        service.store.update(record)
        raw = service.store.root / "raw"
        raw.mkdir(exist_ok=True)
        columns = list(samples[0])
        with (raw / (record["id"] + ".csv")).open("w", encoding="utf-8-sig",
                                                  newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(samples)
        return record


class StrokeRequest(BaseModel):
    glyph: str
    stroke: int = Field(ge=0, lt=10)


class StrokeSaveRequest(BaseModel):
    writer_id: str = "writer-01"
    mount_id: str = "mount-01"


class StrokeScoreRequest(BaseModel):
    glyph: str
    stroke: int = Field(ge=0, lt=10)
    points: list[list[float]] = Field(min_length=1, max_length=5000)
    duration_s: float = 0.0
    endpoint_xy_mm: float = 0.0
    axis_change_deg: float | None = None
    axis_label: str | None = None
    segment_count: int = 1
    reference: dict | None = None

    def as_record(self):
        return {"points": self.points, "duration_s": self.duration_s,
                "summary": {"endpoint_xy_mm": self.endpoint_xy_mm},
                "axis_change_deg": self.axis_change_deg or 0.0,
                "axis_label": self.axis_label or "",
                "segment_count": self.segment_count}
