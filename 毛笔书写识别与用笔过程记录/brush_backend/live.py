"""单机实时会话：采样保存与浏览器刷新解耦，模拟流始终标为 synthetic。"""
import threading
import time

from .schemas import RecordInfo
from .service import AppError


class LiveSessions:
    KEY = "live:active_record"

    def __init__(self, service):
        self.service = service
        self.cancel = threading.Event()
        self.worker = None

    def current(self):
        value = self.service.store.setting(self.KEY)
        return self.service.get(value["id"]) if value else None

    def start(self, info, simulated=False):
        with self.service.store.lock:
            current = self.current()
            if current and current["status"] == "recording":
                raise AppError("已有进行中的实时记录，请先结束或恢复它。", 409)
            plan = None
            if simulated:
                # 仅为演示软件链路复用合成信号，不使用真实用户数据。
                demo = self.service.demo()
                plan = [dict(row, sample_us=1000000 + 2 * (row["sample_us"] - 1000000),
                             ax_g=row["ax_g"] + time.time_ns() % 100000 / 1e9)
                        for row in self.service.store.samples(demo["id"])]
                info = RecordInfo(writer_id="demo-writer", mount_id="demo-mount", title="模拟实时书写 · 合成信号")
            record = self.service.create(info, source="synthetic" if simulated else "real")
            record["input_kind"] = "live-simulation" if simulated else "live-hardware"
            self.service.store.update(record)
            self.service.store.setting(self.KEY, {"id": record["id"]})
            self.cancel = threading.Event()
            if plan:
                self.worker = threading.Thread(target=self._simulate, args=(record["id"], plan, self.cancel), daemon=True)
                self.worker.start()
            return record

    def _simulate(self, record_id, rows, cancel):
        started = time.monotonic()
        try:
            for index in range(0, len(rows), 5):
                batch = rows[index:index + 5]
                due = (batch[-1]["sample_us"] - rows[0]["sample_us"]) / 1e6
                if cancel.wait(max(0, started + due - time.monotonic())):
                    break
                self.service.append(record_id, batch)
        except AppError as error:
            if error.status != 409:
                self._error(record_id)
        except Exception:
            self._error(record_id)
        finally:
            self.service.stop(record_id)

    def _error(self, record_id):
        with self.service.store.lock:
            record = self.service.get(record_id)
            record["live_error"] = "模拟发送中断，请检查本机服务后重新采集。"
            self.service.store.update(record)

    def stop(self, record_id):
        with self.service.store.lock:
            current = self.current()
            if current and current["id"] == record_id:
                self.cancel.set()
            return self.service.stop(record_id)

    def close(self):
        self.cancel.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=2)

    def feed(self, record_id, after, limit):
        # 同一个锁覆盖元数据和分页读取，避免结束状态先于最后一批样本可见。
        with self.service.store.lock:
            record = self.service.get(record_id)
            if after > record["sample_count"]:
                raise AppError("游标超过样本数，请从 0 重新读取。")
            rows = self.service.store.sample_page(record_id, after, limit)
            return {"record": record, "samples": rows, "next_cursor": after + len(rows),
                    "total_count": record["sample_count"], "server_unix_s": time.time()}
