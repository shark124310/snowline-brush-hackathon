"""Write named IMU recordings. Callers serialize access with the link lock."""

import csv
import re
import secrets
from datetime import datetime, timezone


NAME = r"\w[\w -]{0,47}"
RECORDING_FILE = re.compile(rf"[0-9]{{8}}-[0-9]{{6}}_{NAME}_[0-9a-f]{{8}}\.csv")
COLUMNS = (
    "recording_name", "boot_id", "seq", "sample_us", "received_at_unix_s", "rssi_dbm",
    "ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps",
    "roll_deg", "pitch_deg", "yaw_deg", "temperature_c", "rest", "accel_used", "clipped",
)


class CsvRecording:
    def __init__(self, directory):
        self.directory = directory
        self.active = None
        self.last_file = max(
            (p.name for p in directory.glob("*.csv") if RECORDING_FILE.fullmatch(p.name)),
            default=None,
        )
        self.message = ""

    def start(self, name):
        name = name.strip() if isinstance(name, str) else ""
        if not re.fullmatch(NAME, name):
            raise ValueError("名称需为 1–48 个字符，以字母、数字或下划线开头，可包含空格和短横线")
        self.directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"{timestamp}_{name}_{secrets.token_hex(4)}.csv"
        handle = (self.directory / filename).open("x", encoding="utf-8-sig", newline="")
        try:
            writer = csv.writer(handle)
            writer.writerow(COLUMNS)
            handle.flush()
        except OSError:
            handle.close()
            raise
        self.handle, self.writer = handle, writer
        self.active = {"name": name, "file": filename, "packets": 0}
        self.message = ""

    def append(self, packet):
        imu = packet.get("imu")
        if not self.active or not imu or imu["state"] != "ready":
            return
        try:
            self.writer.writerow((
                self.active["name"], packet["boot_id"], packet["seq"], imu["t_us"],
                packet["received_at"], packet["rssi_dbm"], *imu["a_g"], *imu["w_dps"],
                *imu["rpy_deg"], imu["temp_c"], int(imu["rest"]),
                int(imu["accel_used"]), int(imu["clipped"]),
            ))
            self.handle.flush()
        except OSError:
            self.finish("写入失败，记录已停止")
        else:
            self.active["packets"] += 1

    def finish(self, reason="已停止保存"):
        if not self.active:
            return
        try:
            self.handle.close()
        except OSError:
            reason = "关闭文件失败，请检查已写入的数据"
        self.last_file = self.active["file"]
        self.message = f"{reason} · 已写入 {self.active['packets']} 包"
        self.active = None

    def snapshot(self):
        return {"active": dict(self.active) if self.active else None,
                "last_file": self.last_file, "message": self.message}
