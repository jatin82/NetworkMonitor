import json
import os
import sqlite3
import sys
import time
import webbrowser
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Condition, Thread
from typing import Callable, Literal, TypeVar
from urllib.parse import parse_qs
from uuid import uuid4

from download import DEFAULT_INTERVAL_SECONDS, INTERVAL_OPTIONS, THRESHOLD_MBPS, measure_download
from local_store import LocalStore, WINDOW_MINUTES
from wlan import Sample, samples


PORT = 8765
PAGE = Path(__file__).with_name("dashboard.html")


def validated_interval(value: object) -> int:
    if type(value) is not int or value not in INTERVAL_OPTIONS:
        raise ValueError(f"Download-test interval must be one of {INTERVAL_OPTIONS} seconds")
    return value


@dataclass(frozen=True)
class Disruption:
    id: str
    disconnected_at: str
    reconnected_at: str | None = None
    duration_seconds: float | None = None
    status: Literal["ongoing", "restored", "interrupted"] = "ongoing"


@dataclass(frozen=True)
class SpeedAlert:
    id: str
    checked_at: str
    download_mbps: float


Record = TypeVar("Record", Disruption, SpeedAlert)


def write_records(path: Path, rows: list[Record]) -> None:
    temp = NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False)
    try:
        with temp as f:
            json.dump([asdict(row) for row in rows], f, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp.name, path)
    finally:
        Path(temp.name).unlink(missing_ok=True)


def read_records(path: Path, record: type[Record]) -> list[Record]:
    if not path.exists():
        write_records(path, [])
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError("expected a JSON array")
        return [record(**row) for row in rows]
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Cannot read {path}: {exc}. File left unchanged.") from exc


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.lock = Condition()
        self.state = "starting"
        self.error: str | None = None
        self.started_tick: float | None = None
        self.last_tick: float | None = None
        self.running = True
        self.connection_epoch = 0
        self.speed_checked_at: str | None = None
        self.speed_mbps: float | None = None
        self.speed_error: str | None = None
        self.speed_low = False
        self.graph_epoch: int | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        self.store = LocalStore(path.with_name("monitor.sqlite3"))
        self.alerts_enabled = bool(self.store.get_setting("alerts_enabled", 1))
        self.interval_seconds = validated_interval(self.store.get_setting("interval_seconds", DEFAULT_INTERVAL_SECONDS))
        self.history = read_records(path, Disruption)
        self.speed_path = path.with_name("speed-alerts.json")
        self.speed_alerts = read_records(self.speed_path, SpeedAlert)
        if any(row.status == "ongoing" for row in self.history):
            # A previous process ended without observing a recovery.
            self._save([replace(row, status="interrupted") if row.status == "ongoing" else row
                        for row in self.history])

    def _save(self, rows: list[Disruption]) -> None:
        write_records(self.path, rows)
        self.history = rows

    def _finish(self, tick: float, reconnected_at: str | None) -> None:
        if self.started_tick is None:
            return
        row = replace(self.history[-1], reconnected_at=reconnected_at,
                      duration_seconds=round(max(0, tick - self.started_tick), 3),
                      status="restored" if reconnected_at else "interrupted")
        self._save([*self.history[:-1], row])
        self.started_tick = None

    def observe(self, sample: Sample) -> str | None:
        with self.lock:
            alert_id = None
            timestamp = sample.at.isoformat(timespec="milliseconds")
            if sample.state == "unavailable":
                if self.last_tick is not None:
                    self._finish(self.last_tick, None)
                if sample.error != self.error:
                    print(f"WLAN monitor unavailable; retrying: {sample.error}", file=sys.stderr)
            elif sample.state == "connected":
                self._finish(sample.tick, timestamp)
                if self.state != "connected":
                    self.connection_epoch += 1
                    self.lock.notify_all()
            elif self.state == "connected":
                alert_id = uuid4().hex
                self._save([*self.history, Disruption(alert_id, timestamp)])
                self.started_tick = sample.tick
            self.state, self.error, self.last_tick = sample.state, sample.error, sample.tick
            return alert_id

    def stop(self) -> None:
        with self.lock:
            self.running = False
            self.lock.notify_all()
            if self.last_tick is not None:
                self._finish(self.last_tick, None)

    def wait_for_speed_test(self, last_started: float | None) -> int | None:
        with self.lock:
            while self.running:
                if self.state == "connected":
                    remaining = 0 if last_started is None else last_started + self.interval_seconds - time.monotonic()
                    if remaining <= 0:
                        return self.connection_epoch
                    self.lock.wait(remaining)
                else:
                    self.lock.wait()
            return None

    def set_alerts_enabled(self, enabled: bool) -> None:
        with self.lock:
            self.store.set_setting("alerts_enabled", enabled)
            self.alerts_enabled = enabled

    def set_interval_seconds(self, value: object) -> None:
        seconds = validated_interval(value)
        with self.lock:
            if seconds != self.interval_seconds:
                self.store.set_setting("interval_seconds", seconds)
                self.interval_seconds = seconds
                self.graph_epoch = None
                self.lock.notify_all()

    def record_speed(self, epoch: int, mbps: float | None, error: str | None) -> SpeedAlert | None:
        with self.lock:
            if not self.running or self.state != "connected" or epoch != self.connection_epoch:
                return None  # Wi-Fi changed during the download; discard the measurement.
            at_ms = time.time_ns() // 1_000_000
            at = datetime.fromtimestamp(at_ms / 1000, timezone.utc).isoformat(timespec="milliseconds")
            self.speed_checked_at, self.speed_mbps, self.speed_error = at, mbps, error
            try:
                self.store.record(at_ms, mbps, self.graph_epoch != epoch, self.interval_seconds)
                self.graph_epoch = epoch
            except sqlite3.Error as exc:
                self.graph_epoch = None
                self.speed_error = "; ".join(filter(None, [error, f"Cannot save graph reading: {exc}"]))
                print(self.speed_error, file=sys.stderr)
            if mbps is None:
                return None
            low = mbps < THRESHOLD_MBPS
            if low and not self.speed_low:
                alert = SpeedAlert(uuid4().hex, at, mbps)
                alerts = [*self.speed_alerts, alert]
                try:
                    write_records(self.speed_path, alerts)
                except OSError as exc:
                    self.speed_error = f"Cannot save speed alert: {exc}"
                    print(self.speed_error, file=sys.stderr)
                    return None
                self.speed_alerts = alerts
                self.speed_low = True
                return alert
            self.speed_low = low
            return None

    def snapshot(self) -> dict:
        with self.lock:
            elapsed = None if self.started_tick is None else max(0, time.monotonic() - self.started_tick)
            return {"state": self.state, "error": self.error, "elapsed_seconds": elapsed,
                    "alerts_enabled": self.alerts_enabled,
                    "speed": {"checked_at": self.speed_checked_at, "download_mbps": self.speed_mbps,
                              "error": self.speed_error, "threshold_mbps": THRESHOLD_MBPS,
                              "interval_seconds": self.interval_seconds, "interval_options": INTERVAL_OPTIONS,
                              "alerts": [asdict(row) for row in reversed(self.speed_alerts)]},
                    "disruptions": [asdict(row) for row in reversed(self.history)]}


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = False
    journal: Journal
    page: bytes


class Dashboard(BaseHTTPRequestHandler):
    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as exc:
            self.log_message("Dashboard connection closed before the request completed: %s", exc)

    def valid_host(self) -> bool:
        port = self.server.server_port
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self.send_error(403)
            return False
        return True

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, DashboardServer)
        if not self.valid_host():
            return
        path, _, query = self.path.partition("?")
        if path == "/":
            body, content_type = server.page, "text/html; charset=utf-8"
        elif path == "/api/status":
            body = json.dumps(server.journal.snapshot()).encode("utf-8")
            content_type = "application/json"
        elif path == "/api/speed-history":
            values = parse_qs(query, keep_blank_values=True).get("minutes", ["60"])
            if len(values) != 1 or values[0] not in {str(n) for n in WINDOW_MINUTES}:
                self.send_error(400, "Unsupported graph timeframe")
                return
            try:
                history = server.journal.store.history(int(values[0]))
            except sqlite3.Error as exc:
                self.log_error("Cannot read speed history: %s", exc)
                self.send_error(503, "Cannot read local speed history")
                return
            body = json.dumps({**history, "threshold_mbps": THRESHOLD_MBPS,
                               "interval_seconds": server.journal.interval_seconds}).encode("utf-8")
            content_type = "application/json"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        server = self.server
        assert isinstance(server, DashboardServer)
        if not self.valid_host():
            return
        if self.path not in ("/api/alerts", "/api/interval"):
            self.send_error(404)
            return
        if self.headers.get("Origin") not in (None, f"http://{self.headers['Host']}"):
            self.send_error(403)
            return
        if self.headers.get_content_type() != "application/json":
            self.send_error(415, "Expected application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 128:
                raise ValueError("Invalid settings body length")
            data = json.loads(self.rfile.read(length))
            if self.path == "/api/interval":
                if not isinstance(data, dict) or set(data) != {"interval_seconds"}:
                    raise ValueError("Expected an interval_seconds setting")
                server.journal.set_interval_seconds(data["interval_seconds"])
            else:
                if not isinstance(data, dict) or set(data) != {"alerts_enabled"} or not isinstance(data["alerts_enabled"], bool):
                    raise ValueError("Expected a boolean alerts_enabled setting")
                server.journal.set_alerts_enabled(data["alerts_enabled"])
        except ValueError as exc:
            self.send_error(400, "Invalid monitor setting")
            self.log_error("Invalid monitor setting: %s", exc)
            return
        except sqlite3.Error as exc:
            self.log_error("Cannot save monitor setting: %s", exc)
            self.send_error(503, "Cannot save monitor setting")
            return
        self.send_response(204)
        self.end_headers()

    def log_request(self, code="-", size="-") -> None:
        if str(code) not in ("200", "204"):
            super().log_request(code, size)


def open_alert(alert_url: str, message: str, open_tab: Callable[[str], bool]) -> None:
    try:
        if open_tab(alert_url):
            return
    except (OSError, webbrowser.Error) as exc:
        print(f"Browser launch failed: {exc}", file=sys.stderr)
    print(f"{message}. Open the alert at {alert_url}", file=sys.stderr)


def process_sample(journal: Journal, sample: Sample, url: str,
                   open_tab: Callable[[str], bool] = webbrowser.open_new_tab) -> None:
    event = journal.observe(sample)
    if event is not None and journal.alerts_enabled:
        open_alert(f"{url}#alert={event}", "Wi-Fi disconnected", open_tab)


def monitor_speed(journal: Journal, url: str, measure: Callable[[], float] = measure_download,
                  open_tab: Callable[[str], bool] = webbrowser.open_new_tab) -> None:
    last_started = None
    while (epoch := journal.wait_for_speed_test(last_started)) is not None:
        last_started = time.monotonic()
        try:
            mbps = measure()
        except (OSError, HTTPException) as exc:
            journal.record_speed(epoch, None, f"Download test failed: {exc}")
        else:
            alert = journal.record_speed(epoch, mbps, None)
            if alert is not None and journal.alerts_enabled:
                open_alert(f"{url}#speed={alert.id}", "Slow internet download", open_tab)


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("This Wi-Fi monitor requires Windows.")
    path = Path(os.environ["LOCALAPPDATA"]) / "NetworkMonitor" / "disruptions.json"
    url = f"http://127.0.0.1:{PORT}/"
    # Reserve the port before touching history so a second launch cannot interrupt the first.
    with DashboardServer(("127.0.0.1", PORT), Dashboard) as server:
        server.page = PAGE.read_bytes()
        server.journal = Journal(path)
        Thread(target=server.serve_forever, daemon=True).start()
        Thread(target=monitor_speed, args=(server.journal, url), daemon=True).start()
        print(f"Dashboard: {url}\nHistory:   {path}\nMonitoring Wi-Fi. Press Ctrl+C to stop.", flush=True)
        try:
            with closing(samples()) as source:
                for sample in source:
                    process_sample(server.journal, sample, url)
        except KeyboardInterrupt:
            print("\nMonitor stopped.")
        finally:
            server.journal.stop()
            server.shutdown()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, sqlite3.Error) as exc:
        sys.exit(f"Monitor could not continue: {exc}")
