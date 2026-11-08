"""Serve the local ESP32 Wi-Fi console using Python's standard library."""

import argparse
import ipaddress
import json
import secrets
import shutil
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

from csv_recording import CsvRecording, RECORDING_FILE
from wifi_link_test import configure_board, read_test_packet


ONLINE_SECONDS = 4
COMMAND_SECONDS = 8
PENDING = ("queued", "sent")
WEB_DIR = Path(__file__).with_name("web")
RECORDINGS_DIR = WEB_DIR.parent.parent / "recordings"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/console.css": ("console.css", "text/css; charset=utf-8"),
    "/console.js": ("console.js", "text/javascript; charset=utf-8"),
}


def read_motion(raw):
    state = raw["state"]
    if state in ("starting", "calibrating", "unavailable", "paused"):
        progress = raw["cal_pct"]
        if type(progress) is not int or not 0 <= progress <= 100:
            raise ValueError("invalid calibration progress")
        return {"state": state, "cal_pct": progress}
    if state != "ready":
        raise ValueError("invalid IMU state")

    timestamp = raw["t_us"]
    if type(timestamp) is not int or not 0 < timestamp < 2**53:
        raise ValueError("invalid sample timestamp")
    result = {"state": state, "t_us": timestamp}
    for key, limit in (("a_g", 17), ("w_dps", 2100), ("rpy_deg", 181)):
        axes = raw[key]
        if (type(axes) is not list or len(axes) != 3 or
                any(type(axis) not in (int, float) or not -limit <= axis <= limit
                    for axis in axes)):
            raise ValueError("invalid IMU axes")
        result[key] = axes
    temperature = raw["temp_c"]
    if type(temperature) not in (int, float) or not -150 <= temperature <= 200:
        raise ValueError("invalid IMU temperature")
    result["temp_c"] = temperature
    for key in ("rest", "accel_used", "clipped"):
        if type(raw[key]) is not bool:
            raise ValueError("invalid IMU flag")
        result[key] = raw[key]
    return result


def advance_command(command, acknowledged, now):
    if not command or command["state"] not in PENDING:
        return None
    if command["state"] == "sent" and acknowledged == command["id"]:
        command["state"] = "ok"
        command["elapsed_ms"] = round((now - command["started"]) * 1000)
        return None
    command["state"] = "sent"
    return command["id"]


class LinkState:
    def __init__(self, recordings_dir=RECORDINGS_DIR):
        self.lock = threading.Lock()
        self.peer = None
        self.boot_id = None
        self.received = self.missed = self.last_seen = 0
        self.history = deque(maxlen=30)
        self.probe = None
        self.capture = None
        self.recording = CsvRecording(recordings_dir)

    def expire_commands(self, now):
        for command in (self.probe, self.capture):
            if (command and command["state"] in PENDING
                    and now - command["started"] >= COMMAND_SECONDS):
                command["state"] = "timeout"

    def online(self, now):
        return bool(self.history) and now - self.last_seen < ONLINE_SECONDS

    def record(self, packet, peer):
        with self.lock:
            now = time.monotonic()
            self.expire_commands(now)
            if (peer, packet["boot_id"]) != (self.peer, self.boot_id):
                self.recording.finish("开发板重启或来源变化，已停止保存")
                self.peer, self.boot_id = peer, packet["boot_id"]
                self.history.clear()
                self.received = self.missed = 0
                self.probe = None
                self.capture = None

            seq = packet["seq"]
            if self.history:
                last_seq = self.history[-1]["seq"]
                if seq <= last_seq:
                    return None, None
                self.missed += seq - last_seq - 1

            self.last_seen = now
            self.received += 1
            packet = {**packet, "received_at": time.time()}
            self.history.append(packet)
            self.recording.append(packet)
            probe = advance_command(self.probe, packet.get("probe_ack"), now)
            control = None
            if self.capture:
                enabled = packet.get("imu", {}).get("state") != "paused"
                ack = packet.get("capture_ack") if enabled == self.capture["enabled"] else None
                capture_id = advance_command(self.capture, ack, now)
                if capture_id is not None:
                    action = "start" if self.capture["enabled"] else "stop"
                    control = f"{capture_id}:{action}"
            return probe, control

    def queue_probe(self):
        with self.lock:
            now = time.monotonic()
            self.expire_commands(now)
            if not self.online(now):
                return None, "开发板离线"
            if "probe_ack" not in self.history[-1]:
                return None, "当前板端固件不支持主动探测"
            if self.probe and self.probe["state"] in PENDING:
                return None, "已有探测在进行"
            self.probe = {"id": secrets.randbelow(999_999_999) + 1,
                          "state": "queued", "started": now}
            return self.probe["id"], None

    def queue_capture(self, enabled):
        with self.lock:
            now = time.monotonic()
            self.expire_commands(now)
            if not self.online(now):
                return None, "开发板离线"
            latest = self.history[-1]
            if "capture_ack" not in latest or "imu" not in latest:
                return None, "当前固件不支持网页启停"
            if self.capture and self.capture["state"] in PENDING:
                return None, "等待上一条启停指令的板端回执"
            self.capture = {"id": secrets.randbelow(999_999_999) + 1,
                            "state": "queued", "started": now, "enabled": enabled}
            return self.capture["id"], None

    def change_recording(self, action, payload):
        with self.lock:
            if action == "start":
                if self.recording.active:
                    return 409, "已有记录正在保存"
                if not self.online(time.monotonic()) or "imu" not in self.history[-1]:
                    return 409, "需要开发板在线并发送 IMU 状态"
                try:
                    self.recording.start(payload.get("name"))
                except ValueError as error:
                    return 400, str(error)
                except OSError:
                    return 500, "创建记录文件失败，请检查电脑的保存目录"
                return 201, None

            if self.recording.active:
                if payload.get("file") != self.recording.active["file"]:
                    return 409, "当前记录已变化，请刷新后再操作"
                self.recording.finish()
            return 200, None

    def recording_path(self, filename):
        if not RECORDING_FILE.fullmatch(filename):
            return None
        with self.lock:
            active = self.recording.active
            if active and active["file"] == filename:
                return None
            return self.recording.directory / filename

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            self.expire_commands(now)
            return {
                "online": self.online(now),
                "peer": self.peer,
                "boot_id": self.boot_id,
                "received": self.received,
                "missed": self.missed,
                "history": list(self.history),
                "probe": dict(self.probe) if self.probe else None,
                "capture": dict(self.capture) if self.capture else None,
                "recording": self.recording.snapshot(),
            }


class ConsoleHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def reply(self, status, data, content_type, probe=None, control=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        if probe is not None:
            self.send_header("X-Link-Probe", str(probe))
        if control is not None:
            self.send_header("X-Capture-Control", control)
        self.end_headers()
        self.wfile.write(data)

    def reply_json(self, status, data):
        self.reply(status, json.dumps(data).encode(), "application/json")

    def read_action(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 1 <= length <= 1024:
            raise ValueError("invalid action length")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("invalid action")
        return data

    def download(self, filename):
        path = self.server.link.recording_path(filename)
        if not path or path.is_symlink() or not path.is_file():
            self.send_error(404)
            return
        with path.open("rb") as source:
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"recording.csv\"; filename*=UTF-8''{quote(filename)}")
            self.end_headers()
            shutil.copyfileobj(source, self.wfile)

    def do_GET(self):
        if self.path == "/api/state":
            self.reply_json(200, self.server.link.snapshot())
        elif self.path.startswith("/api/recordings/"):
            self.download(unquote(self.path.removeprefix("/api/recordings/")))
        elif self.path in ASSETS:
            name, content_type = ASSETS[self.path]
            self.reply(200, (WEB_DIR / name).read_bytes(), content_type)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/probe":
            if self.headers.get_content_type() != "application/json":
                self.send_error(415)
                return
            probe, error = self.server.link.queue_probe()
            self.reply_json(409 if error else 202, {"id": probe, "error": error})
        elif self.path in ("/api/capture", "/api/recording"):
            if self.headers.get_content_type() != "application/json":
                self.reply_json(415, {"error": "需要 JSON 请求"})
                return
            try:
                payload = self.read_action()
                if self.path == "/api/capture" and type(payload.get("enabled")) is bool:
                    capture, error = self.server.link.queue_capture(payload["enabled"])
                    self.reply_json(409 if error else 202, {"id": capture, "error": error})
                elif self.path == "/api/recording" and payload.get("action") in ("start", "stop"):
                    status, error = self.server.link.change_recording(payload["action"], payload)
                    self.reply_json(status, {"error": error})
                else:
                    self.reply_json(400, {"error": "无效操作"})
            except ValueError:
                self.reply_json(400, {"error": "无效请求"})
        elif self.path == "/api/wifi-test":
            try:
                raw = read_test_packet(self.headers, self.rfile)
                # Store only fields belonging to the Wi-Fi test protocol.
                packet = {key: raw[key] for key in ("seq", "boot_id", "rssi_dbm",
                                                   "uptime_ms", "probe_ack", "capture_ack") if key in raw}
                for key in ("uptime_ms", "probe_ack", "capture_ack"):
                    if key in packet and (type(packet[key]) is not int or
                                          not 0 <= packet[key] <= 0xFFFFFFFF):
                        raise ValueError("invalid telemetry")
                if "imu" in raw:
                    packet["imu"] = read_motion(raw["imu"])
                if "oled" in raw:
                    if type(raw["oled"]) is not bool:
                        raise ValueError("invalid OLED status")
                    packet["oled"] = raw["oled"]
            except (KeyError, TypeError, ValueError):
                self.send_error(400, "Invalid Wi-Fi test packet")
                return
            probe, control = self.server.link.record(packet, self.client_address[0])
            self.reply(200, str(packet["seq"]).encode(), "text/plain", probe, control)
        else:
            self.send_error(404)

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, type=ipaddress.IPv4Address,
                        help="IPv4 address of the PC interface on the board's LAN")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--serial-port", help="Provision Wi-Fi over UART at startup, for example COM8")
    args = parser.parse_args()
    if args.host.is_unspecified or not 1 <= args.port <= 65535:
        parser.error("Specify a LAN IPv4 address and a valid TCP port")

    try:
        with ThreadingHTTPServer((str(args.host), args.port), ConsoleHandler) as server:
            server.link = LinkState()
            base_url = f"http://{args.host}:{args.port}"
            if args.serial_port:
                with configure_board(args.serial_port, base_url + "/api/wifi-test"):
                    pass
            print(f"Web console: {base_url}/", flush=True)
            try:
                server.serve_forever()
            finally:
                with server.link.lock:
                    server.link.recording.finish("上位机已关闭")
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as error:
        parser.exit(1, f"Web console failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
