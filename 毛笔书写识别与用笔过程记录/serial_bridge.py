"""USB 串口 → 本机批量接口；不负责 IMU 采样、融合或距离估计。"""
import argparse
import csv
import json
import os
import sys
import time

import httpx

from brush_backend.schemas import Sample


class LineParser:
    def __init__(self, mode="jsonl", columns=None):
        self.mode, self.columns = mode, columns

    def parse(self, line):
        line = line.strip()
        if not line or line.startswith("#"):
            return None
        if self.mode == "jsonl":
            row = json.loads(line)
        else:
            values = next(csv.reader([line]))
            if "sample_us" in values:
                if len(set(values)) != len(values):
                    raise ValueError("CSV 表头有重复列")
                self.columns = [v.strip() for v in values]
                return None
            if not self.columns:
                raise ValueError("CSV 需要先发送表头或用 --columns 指定列名")
            if len(values) != len(self.columns):
                raise ValueError("串口列数与表头不一致")
            row = {key: value.strip() for key, value in zip(self.columns, values) if value.strip()}
        return Sample.model_validate(row).model_dump(exclude_none=True)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="实时串口桥：先启动网页记录，再转发新采样")
    parser.add_argument("--port", help="例如 COM5；使用 --list 查看")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    parser.add_argument("--columns", help="无表头 CSV 的逗号分隔列名，必须与固件一致")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        parser.error("请先安装：python -m pip install -r requirements-hardware.txt")
    if args.list:
        for port in list_ports.comports():
            print(port.device, port.description)
        return
    if not args.port:
        parser.error("请指定 --port COMx；可先运行 --list")
    columns = [c.strip() for c in args.columns.split(",")] if args.columns else None
    decoder = LineParser(args.format, columns)
    headers = {}
    if os.environ.get("BRUSH_API_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["BRUSH_API_TOKEN"]
    base = args.url.rstrip("/") + "/api/v1/brush"
    # 避开 Windows 系统代理，保护本机实时链路。
    with httpx.Client(timeout=5, trust_env=False, headers=headers) as client, serial.Serial(args.port, args.baud, timeout=.02) as device:
        active_id, pending, sent = None, [], 0
        next_check, last_send = 0., time.monotonic()
        errors = 0
        print("串口已打开。请在网页点击‘开始真实记录’；尚未开始时丢弃串口旧数据。")
        while True:
            now = time.monotonic()
            if now >= next_check:
                response = client.get(base + "/live/current")
                response.raise_for_status()
                record = response.json()["data"]
                session = record if record and record["status"] == "recording" and record["source_kind"] == "real" else None
                new_id = session["id"] if session else None
                if new_id != active_id:
                    if active_id:
                        print(f"记录已结束，已确认上传 {sent} 点；界面结束后到达的数据不计入该段。")
                    active_id, pending, sent = new_id, [], 0
                    if active_id:
                        device.reset_input_buffer()
                        decoder.buffer = b""
                        print("开始转发：", active_id, "；等网页显示收到数据后再落笔。")
                next_check = now + .2
            raw = device.read_until(b"\n", size=16384)
            # 超时可能返回半行，保存后续拼接；上限约束异常固件输出。
            if not hasattr(decoder, "buffer"):
                decoder.buffer = b""
            decoder.buffer += raw
            while b"\n" in decoder.buffer:
                line, decoder.buffer = decoder.buffer.split(b"\n", 1)
                try:
                    sample = decoder.parse(line.decode("utf-8"))
                    if active_id and sample:
                        pending.append(sample)
                except (ValueError, TypeError):
                    errors += 1
                    if errors == 1 or errors % 100 == 0:
                        print(f"累计 {errors} 行无法解析；请核对格式、表头和单位，缺样将体现在质量检查中。")
            if len(decoder.buffer) > 16384:
                raise RuntimeError("串口单行超过 16 KB，请核对固件是否逐行输出")
            if active_id and pending and (now - last_send >= .08 or len(pending) >= 100):
                batch = pending[:1000]
                result = client.post(base + "/records/" + active_id + "/samples", json={"samples": batch})
                if result.status_code == 409:
                    active_id, pending = None, []
                    next_check = 0
                    continue
                # 不盲目重试不确定是否提交的批次，避免悄悄重复采样。
                result.raise_for_status()
                del pending[:len(batch)]
                sent += len(batch)
                last_send = now


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("串口桥已停止，请在网页结束当前记录。")
    except (httpx.HTTPError, OSError, RuntimeError) as error:
        print("串口桥已停止：", type(error).__name__, "。请检查串口和服务，结束本段后重新采集；未确认批次不会自动重发。")
        raise SystemExit(1)
