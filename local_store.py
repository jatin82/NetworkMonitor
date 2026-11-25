import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Generator


WINDOW_MINUTES = (15, 60, 360, 1440)
DAY_MS = max(WINDOW_MINUTES) * 60_000


class LocalStore:
    def __init__(self, path: Path):
        self.path = path
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS readings (at_ms INTEGER NOT NULL, mbps REAL, new_segment INTEGER NOT NULL)")
            if "interval_seconds" not in {row[1] for row in db.execute("PRAGMA table_info(readings)")}:
                # Readings written before intervals were configurable used 30-second checks.
                db.execute("ALTER TABLE readings ADD COLUMN interval_seconds INTEGER NOT NULL DEFAULT 30")
            db.execute("CREATE INDEX IF NOT EXISTS readings_time ON readings(at_ms)")
            db.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            db.execute("DELETE FROM readings WHERE at_ms < ?", (time.time_ns() // 1_000_000 - DAY_MS,))

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            yield db

    def get_setting(self, name: str, default: int) -> int:
        with self.connection() as db:
            row = db.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return default if row is None else row[0]

    def set_setting(self, name: str, value: int) -> None:
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (name, value))

    def record(self, at_ms: int, mbps: float | None, new_segment: bool, interval_seconds: int) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO readings (at_ms, mbps, new_segment, interval_seconds) VALUES (?, ?, ?, ?)",
                       (at_ms, mbps, new_segment, interval_seconds))
            db.execute("DELETE FROM readings WHERE at_ms < ?", (at_ms - DAY_MS,))

    def history(self, minutes: int) -> dict:
        end = time.time_ns() // 1_000_000
        start = end - minutes * 60_000
        with self.connection() as db:
            rows = db.execute("SELECT at_ms, mbps, new_segment, interval_seconds FROM readings WHERE at_ms BETWEEN ? AND ? "
                              "ORDER BY at_ms, rowid", (start, end)).fetchall()
        return {"start_ms": start, "end_ms": end,
                "samples": [{"at": at, "mbps": rate, "new_segment": bool(new), "interval_seconds": interval}
                            for at, rate, new, interval in rows]}
