from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


class Store:
    """本机单进程存储；一批样本与元数据在同一事务落盘。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.database = self.root / "brush.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY, metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS samples (
                    record_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(record_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, metadata: dict, samples: list[dict]):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO records VALUES (?, ?)", (metadata["id"], json.dumps(metadata, ensure_ascii=False)))
            self._append(db, metadata["id"], 0, samples)

    @staticmethod
    def _append(db, record_id, start, samples):
        db.executemany("INSERT INTO samples VALUES (?, ?, ?)", [
            (record_id, start + i, json.dumps(sample, ensure_ascii=False, allow_nan=False))
            for i, sample in enumerate(samples)
        ])

    def get(self, record_id: str):
        with self.connect() as db:
            row = db.execute("SELECT metadata FROM records WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(record_id)
        return json.loads(row[0])

    def list(self):
        with self.connect() as db:
            rows = db.execute("SELECT metadata FROM records ORDER BY rowid DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    def samples(self, record_id: str):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM samples WHERE record_id = ? ORDER BY ordinal", (record_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def sample_page(self, record_id: str, after: int, limit: int):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM samples WHERE record_id = ? AND ordinal >= ? ORDER BY ordinal LIMIT ?",
                              (record_id, after, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update(self, metadata: dict):
        with self.lock, self.connect() as db:
            db.execute("UPDATE records SET metadata = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False, allow_nan=False), metadata["id"]))

    def append(self, metadata: dict, samples: list[dict]):
        with self.lock, self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM samples WHERE record_id = ?", (metadata["id"],)).fetchone()[0]
            self._append(db, metadata["id"], count, samples)
            db.execute("UPDATE records SET metadata = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False), metadata["id"]))

    def setting(self, key: str, value=None):
        with self.lock, self.connect() as db:
            if value is not None:
                db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False, allow_nan=False)))
                return value
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
