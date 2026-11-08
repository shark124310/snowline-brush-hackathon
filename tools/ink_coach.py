"""Serve the local calligraphy practice station and its USB capture bridge."""

import argparse
import csv
import json
import math
import threading
import uuid
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shutil import copyfileobj
from statistics import median
from tempfile import SpooledTemporaryFile
from urllib.parse import quote, unquote
from zipfile import ZIP_DEFLATED, ZipFile

from imu_trace import (
    ANCHOR_SECONDS, HOLD_CUE_SECONDS, RAW_HEADER, WRITE_CUE_SECONDS, capture,
    median_acceleration, reconstruct, rotate, smooth_median, trajectory_summary, validate,
)


WEB = Path(__file__).with_name("ink_web")
RECORDINGS = Path(__file__).resolve().parents[1] / "recordings" / "ink-coach"
START_SETTLE_SECONDS = 0.45
ANCHOR_GYRO_DPS = 5.0
ANCHOR_JITTER_MPS2 = 0.4
ANCHOR_MAX_BIAS_MPS2 = 1.5
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/ink.css": ("ink.css", "text/css; charset=utf-8"),
    "/ink.js": ("ink.js", "text/javascript; charset=utf-8"),
    "/sample.json": ("sample.json", "application/json"),
}
GLYPHS = {
    "三": ("第一横", "第二横", "第三横"),
    "十": ("横", "竖"),
    "口": ("竖", "横折", "封口横"),
}


def select_anchor(samples, times, first, last, label):
    best = None
    for i in range(bisect_left(times, first), bisect_right(times, last - ANCHOR_SECONDS)):
        stop = bisect_right(times, times[i] + ANCHOR_SECONDS)
        window = samples[i:stop]
        bias = median_acceleration(window)
        if math.hypot(*bias) > ANCHOR_MAX_BIAS_MPS2:
            continue
        activity = sorted(max(sample.gyro / ANCHOR_GYRO_DPS,
                              math.dist(sample.accel, bias) / ANCHOR_JITTER_MPS2)
                          for sample in window)
        score = activity[math.ceil(len(window) * 0.85) - 1]
        if score <= 1.0 and (best is None or score < best[0]):
            best = score, range(i, stop), bias
    if best is None:
        raise ValueError(f"{label}未找到稳定段，请等书写提示再动笔，写完停住")
    return best[1], best[2]


def guided_stroke(samples):
    times = [(sample.us - samples[0].us) / 1e6 for sample in samples]
    if times[-1] <= HOLD_CUE_SECONDS:
        raise ValueError("采样时间太短，无法分析整笔动作")
    head, start_bias = select_anchor(samples, times, START_SETTLE_SECONDS, WRITE_CUE_SECONDS, "动笔前")
    tail, end_bias = select_anchor(samples, times, times[-1] - 1.0, times[-1], "收笔后")
    first = (times[head.start] + times[head.stop - 1]) / 2
    last = (times[tail.start] + times[tail.stop - 1]) / 2
    corrected = []
    for sample, t in zip(samples, times):
        alpha = max(0.0, min(1.0, (t - first) / (last - first)))
        bias = tuple(a + (b - a) * alpha for a, b in zip(start_bias, end_bias))
        corrected.append(tuple(value - base for value, base in zip(sample.accel, bias)))
    linear = smooth_median([math.hypot(*accel) for accel in corrected], radius=3)
    gyro = smooth_median([sample.gyro for sample in samples], radius=3)
    quiet = [*head, *tail]
    threshold = max(0.08, 4 * median(linear[i] for i in quiet))
    gyro_threshold = max(6.0, 3 * median(gyro[i] for i in quiet))

    # Allow the user time to react to the on-screen hold cue.
    stop = min(tail.start, bisect_right(times, HOLD_CUE_SECONDS + 1.25))
    groups = []
    for i in range(bisect_left(times, WRITE_CUE_SECONDS), stop):
        if linear[i] <= threshold and gyro[i] <= gyro_threshold:
            continue
        if not groups or times[i] - times[groups[-1][-1]] > 0.4:
            groups.append([])
        groups[-1].append(i)
    groups = [group for group in groups if len(group) >= 6]
    if not groups:
        raise ValueError("未在书写时段找到明显笔画，请等提示后写一笔")
    primary = max(groups, key=len)
    start, end = times[primary[0]], times[primary[-1]]
    if end - start > 3.0:
        raise ValueError("检测到持续动作，请按提示只写一笔")
    first_index = bisect_left(times, start - 0.10)
    last_index = bisect_right(times, end + 0.10)
    segment = [replace(samples[i], accel=corrected[i]) for i in range(first_index, last_index)]
    return segment, threshold, len(groups)


def pack_result(segment, positions, report, axis, axis_label, source, segment_count):
    start_us = segment[0].us
    points = []
    direction = rotate(segment[0].q, axis)
    min_dot = 1.0
    for sample, (x, y, _) in zip(segment, positions):
        rotated = rotate(sample.q, axis)
        min_dot = min(min_dot, sum(a * b for a, b in zip(direction, rotated)))
        z = abs(rotated[2])
        tilt = math.degrees(math.acos(min(1.0, z)))
        points.append([round((sample.us - start_us) / 1e6, 4),
                       round(x, 3), round(y, 3), round(tilt, 2)])
    return {
        "source": source,
        "axis_label": axis_label,
        "axis_change_deg": round(math.degrees(math.acos(max(-1.0, min(1.0, min_dot)))), 2),
        "segment_count": segment_count,
        "duration_s": points[-1][0],
        "summary": report,
        "points": points,
    }


def open_board(name):
    import serial

    port = serial.Serial(baudrate=115200, timeout=0.08, write_timeout=2)
    port.port = name
    port.dtr = False
    port.rts = False
    return port


class CaptureStation:
    def __init__(self, port, tip_mm, directory=RECORDINGS, board_factory=open_board):
        self.port = port
        self.tip_mm = tip_mm
        self.directory = directory
        self.board_factory = board_factory
        self.lock = threading.Lock()
        self.jobs = deque(maxlen=8)

    def status(self, job_id=None):
        with self.lock:
            for job in reversed(self.jobs):
                if job_id is None or job["id"] == job_id:
                    return dict(job)
            return None

    def update(self, phase, message, **fields):
        with self.lock:
            self.jobs[-1].update(phase=phase, message=message, **fields)

    def start(self, glyph, stroke):
        with self.lock:
            if self.jobs and self.jobs[-1]["phase"] not in ("done", "error"):
                return None
            job_id = uuid.uuid4().hex[:12]
            self.jobs.append({"id": job_id, "glyph": glyph, "stroke": stroke,
                              "phase": "opening", "message": "正在连接 USB…"})
        threading.Thread(target=self.record, args=(job_id, glyph, stroke), daemon=True).start()
        return job_id

    def record(self, job_id, glyph, stroke):
        key = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + job_id
        folder = self.stroke_directory(glyph, stroke) / key
        raw_path = folder / "imu.csv"
        try:
            folder.mkdir(parents=True)
            pending = folder / "imu.part"
            with self.board_factory(self.port) as board:
                samples = capture(board, pending, cue=f"书写{GLYPHS[glyph][stroke]}",
                                  on_progress=self.update, wait_for_stationary=False)
            pending.rename(raw_path)
            self.update("analyzing", "正在重建轨迹…")
            report = validate(samples)
            segment, threshold, segment_count = guided_stroke(samples)
            positions = reconstruct(segment, self.tip_mm)
            if math.hypot(*positions[-1][:2]) < 2:
                raise ValueError("检测到的平面位移太小，请放大字幅后重录")
            report.update(trajectory_summary(segment, positions), motion_threshold_mps2=round(threshold, 4))
            tip_norm = math.hypot(*self.tip_mm)
            axis = tuple(x / tip_norm for x in self.tip_mm) if tip_norm else (0, 0, 1)
            result = pack_result(segment, positions, report, axis,
                                 "笔尖偏移轴" if tip_norm else "IMU Z 轴",
                                 "USB 实板", segment_count)
            result.update(glyph=glyph, stroke=stroke, recording=key)
            with (folder / "stroke.json").open("x", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
            self.update("done", "本笔已保存，可以回放。", result=result)
        except (OSError, RuntimeError, ValueError, ImportError) as error:
            message = str(error)
            if message == "TRACE_ERROR IMU not ready or trace busy":
                message = "IMU 尚未就绪或正在采样。请等板载屏幕显示姿态后重试。"
            if raw_path.is_file():
                message += " 本笔的采样 CSV 已保存。"
            self.update("error", message)

    def stroke_directory(self, glyph, stroke):
        return self.directory / glyph / f"{stroke + 1:02d}-{GLYPHS[glyph][stroke]}"

    def write_raw_archive(self, glyph, output):
        entries = []
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            for stroke, label in enumerate(GLYPHS[glyph]):
                directory = self.stroke_directory(glyph, stroke)
                for path in sorted(directory.glob("*/imu.csv")):
                    with path.open(encoding="utf-8", newline="") as handle:
                        if tuple(next(csv.reader(handle), ())) != RAW_HEADER:
                            continue
                    name = f"{directory.name}/{path.parent.name}.csv"
                    archive.write(path, name)
                    entries.append({"stroke": stroke + 1, "label": label, "file": name})
            if entries:
                manifest = {"character": glyph, "source": "MPU6050",
                            "gyro_raw_columns": list(RAW_HEADER[-3:]),
                            "gyro_lsb_per_dps": 65.5,
                            "sample_us": "microseconds since ESP32 boot",
                            "recordings": entries}
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return bool(entries)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(4)

    def reply(self, status, content_type, data):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def json(self, status, data):
        self.reply(status, "application/json", json.dumps(data, ensure_ascii=False).encode())

    def download_raw(self, glyph):
        if glyph not in GLYPHS:
            self.json(404, {"error": "找不到该字"})
            return
        with SpooledTemporaryFile(max_size=2 * 1024 * 1024) as output:
            if not self.server.station.write_raw_archive(glyph, output):
                self.json(404, {"error": "本字还没有三轴原始数据。请先在实板采集模式录制。"})
                return
            length = output.tell()
            output.seek(0)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = quote(f"腕墨-{glyph}-原始数据-{stamp}.zip")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{filename}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            copyfileobj(output, self.wfile)

    def do_GET(self):
        if self.path in ASSETS:
            name, kind = ASSETS[self.path]
            self.reply(200, kind, (WEB / name).read_bytes())
        elif self.path == "/api/station":
            self.json(200, {"port": self.server.station.port, "job": self.server.station.status()})
        elif self.path.startswith("/api/raw/"):
            self.download_raw(unquote(self.path.removeprefix("/api/raw/")))
        elif self.path.startswith("/api/stroke/"):
            job = self.server.station.status(self.path.removeprefix("/api/stroke/"))
            self.json(200, job) if job else self.json(404, {"error": "找不到本次采集"})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/stroke":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 256 or self.headers.get_content_type() != "application/json":
                raise ValueError()
            payload = json.loads(self.rfile.read(length))
            glyph, stroke = payload["glyph"], payload["stroke"]
            if glyph not in GLYPHS or type(stroke) is not int or not 0 <= stroke < len(GLYPHS[glyph]):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            self.json(400, {"error": "选字或笔画无效"})
            return
        job_id = self.server.station.start(glyph, stroke)
        if job_id is None:
            self.json(409, {"error": "已有采集正在进行，请等它完成"})
        else:
            self.json(202, {"id": job_id})

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--serial-port", default="COM10")
    parser.add_argument("--tip-mm", type=float, nargs=3, default=(0, 0, 0),
                        metavar=("X", "Y", "Z"))
    args = parser.parse_args()
    if not all(map(math.isfinite, args.tip_mm)):
        parser.error("--tip-mm 需要三个有限值")
    try:
        with ThreadingHTTPServer((args.host, args.port), Handler) as server:
            server.station = CaptureStation(args.serial_port, args.tip_mm)
            print(f"腕墨 · http://{args.host}:{server.server_port}/", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
