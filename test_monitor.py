import ctypes as ct
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from io import BytesIO
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import MagicMock, Mock, patch

import download
import wlan
from local_store import LocalStore
from monitor import Dashboard, DashboardServer, Journal, monitor_speed, process_sample


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
URL = "http://127.0.0.1:8765/"


def sample(state: wlan.Link, seconds: float) -> wlan.Sample:
    return wlan.Sample(state, START + timedelta(seconds=seconds), seconds)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "disruptions.json"
        self.journal = Journal(self.path)

    def records(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_brief_drop_is_saved_before_alert_and_completed_only_once(self):
        def open_tab(url):
            event = self.records()[-1]
            self.assertEqual(url, f"{URL}#alert={event['id']}")
            self.assertEqual(event["status"], "ongoing")
            self.assertIsNone(event["reconnected_at"])
            return True

        browser = Mock(side_effect=open_tab)
        for state, at in [("connected", 0), ("disconnected", 1), ("disconnected", 1.1),
                          ("connected", 1.25), ("connected", 2)]:
            process_sample(self.journal, sample(state, at), URL, browser)
        browser.assert_called_once()
        [row] = self.records()
        self.assertEqual((row["disconnected_at"], row["reconnected_at"], row["duration_seconds"]),
                         ("2026-01-01T00:00:01.000+00:00", "2026-01-01T00:00:01.250+00:00", 0.25))
        self.assertEqual(row["status"], "restored")
        self.assertEqual(Journal(self.path).snapshot()["disruptions"], [row])
        process_sample(self.journal, sample("disconnected", 3), URL, browser)
        self.assertEqual(browser.call_count, 2)
        self.assertEqual(self.journal.snapshot()["disruptions"], list(reversed(self.records())))

    def test_starting_offline_and_losing_observation_are_not_disconnects(self):
        for state, at in [("no-adapter", 0), ("disconnected", 1), ("connected", 2),
                          ("unavailable", 3), ("disconnected", 5)]:
            self.assertIsNone(self.journal.observe(sample(state, at)))
        self.assertEqual(self.records(), [])
        self.journal.observe(sample("connected", 6))
        self.assertIsNotNone(self.journal.observe(sample("no-adapter", 7)))
        self.journal.observe(sample("disconnected", 9))
        self.journal.stop()
        [row] = self.records()
        self.assertEqual((row["status"], row["duration_seconds"], row["reconnected_at"]),
                         ("interrupted", 2, None))

    def test_restart_does_not_count_time_that_was_not_monitored(self):
        self.journal.observe(sample("connected", 0))
        self.journal.observe(sample("disconnected", 1))
        restarted = Journal(self.path)
        restarted.observe(sample("connected", 100))
        [row] = self.records()
        self.assertEqual((row["status"], row["reconnected_at"], row["duration_seconds"]),
                         ("interrupted", None, None))

    def test_unreadable_history_is_not_replaced(self):
        self.path.write_text("[", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "File left unchanged"):
            Journal(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[")

    def test_speed_alerts_only_on_breach_and_rearm_at_one_mbps(self):
        self.journal.observe(sample("connected", 0))
        epoch = self.journal.wait_for_speed_test(None)
        readings = [2, 0.9999, TimeoutError("timed out"), 0.5, 1.0, 0.7]
        browser = Mock(return_value=True)
        with patch.object(self.journal, "wait_for_speed_test", side_effect=[epoch] * len(readings) + [None]):
            monitor_speed(self.journal, URL, Mock(side_effect=readings), browser)
        history = json.loads(self.journal.speed_path.read_text(encoding="utf-8"))
        self.assertEqual([row["download_mbps"] for row in history], [0.9999, 0.7])
        self.assertEqual([call.args[0] for call in browser.call_args_list],
                         [f"{URL}#speed={row['id']}" for row in history])
        self.assertTrue(all(datetime.fromisoformat(row["checked_at"]).utcoffset() == timedelta(0)
                            for row in history))
        self.assertEqual(self.records(), [])
        self.assertEqual(Journal(self.path).snapshot()["speed"]["alerts"], list(reversed(history)))

    def test_failed_or_disconnected_test_is_not_a_speed_measurement(self):
        self.journal.observe(sample("connected", 0))
        epoch = self.journal.connection_epoch
        with patch.object(self.journal, "wait_for_speed_test", side_effect=[epoch, None]):
            monitor_speed(self.journal, URL, Mock(side_effect=OSError("test endpoint unreachable")))
        result = self.journal.snapshot()["speed"]
        self.assertIn("unreachable", result["error"])
        self.assertIsNone(result["download_mbps"])
        self.journal.observe(sample("disconnected", 1))
        self.journal.observe(sample("connected", 2))
        self.assertIsNone(self.journal.record_speed(epoch, 0.1, None))
        self.assertEqual(self.journal.snapshot()["speed"], result)
        self.assertEqual(result["alerts"], [])
        self.assertEqual([p["mbps"] for p in self.journal.store.history(15)["samples"]], [None])

    def test_graph_windows_retention_and_restart(self):
        end = int(START.timestamp() * 1000)
        self.journal.observe(sample("connected", 0))
        # Include a point on each left boundary, one expired point, and a failed test.
        readings = [(1441, 90), (1440, 24), (360, 6), (60, 3), (16, 2), (15, 1.5), (1, None), (0, 4)]
        for age, rate in readings:
            with patch("monitor.time.time_ns", return_value=(end - age * 60_000) * 1_000_000):
                self.journal.record_speed(self.journal.connection_epoch, rate, "timeout" if rate is None else None)
        with patch("monitor.time.time_ns", return_value=end * 1_000_000):
            restarted = Journal(self.path)
            for minutes in (15, 60, 360, 1440):
                window = restarted.store.history(minutes)
                self.assertEqual((window["start_ms"], window["end_ms"]), (end - minutes * 60_000, end))
                self.assertEqual([(p["at"], p["mbps"]) for p in window["samples"]],
                                 [(end - age * 60_000, rate) for age, rate in readings if age <= minutes])
            with restarted.store.connection() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM readings").fetchone()[0], len(readings) - 1)
            restarted.observe(sample("connected", 1))
            restarted.record_speed(restarted.connection_epoch, 7, None)
            points = restarted.store.history(15)["samples"]
            self.assertTrue(points[-1]["new_segment"])
            self.assertFalse(points[-2]["new_segment"])

    def test_muting_survives_restart_and_neither_alert_type_stops_logging(self):
        self.journal.set_alerts_enabled(False)
        restarted = Journal(self.path)
        self.assertFalse(restarted.snapshot()["alerts_enabled"])
        browser = Mock(return_value=True)
        for cycle in range(2):
            for state in ("connected", "disconnected", "connected"):
                process_sample(restarted, sample(state, cycle * 10), URL, browser)
            epoch = restarted.connection_epoch
            with patch.object(restarted, "wait_for_speed_test", side_effect=[epoch, epoch, None]):
                monitor_speed(restarted, URL, Mock(side_effect=[2, 0.75]), browser)
            if cycle == 0:
                browser.assert_not_called()
                restarted.set_alerts_enabled(True)
        self.assertEqual([call.args[0].split("#")[1].split("=")[0] for call in browser.call_args_list],
                         ["alert", "speed"])
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(len(restarted.snapshot()["speed"]["alerts"]), 2)
        self.assertEqual([p["mbps"] for p in restarted.store.history(15)["samples"]], [2, 0.75, 2, 0.75])
        self.assertTrue(Journal(self.path).alerts_enabled)

    def test_dashboard_history_and_alert_control_api(self):
        with DashboardServer(("127.0.0.1", 0), Dashboard) as server:
            server.journal = self.journal
            Thread(target=server.serve_forever, daemon=True).start()
            try:
                with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=3)) as client:
                    client.request("GET", "/api/speed-history?minutes=15")
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    window = json.load(response)
                    self.assertEqual(window["end_ms"] - window["start_ms"], 900_000)
                    self.assertEqual(window["samples"], [])
                    client.request("GET", "/api/speed-history?minutes=0")
                    self.assertEqual(client.getresponse().status, 400)
                    headers = {"Content-Type": "application/json"}
                    body = '{"alerts_enabled":false}'
                    client.request("POST", "/api/alerts", body, {**headers, "Origin": "http://example.invalid"})
                    self.assertEqual(client.getresponse().status, 403)
                    client.request("POST", "/api/alerts", '{"alerts_enabled":"false"}', headers)
                    self.assertEqual(client.getresponse().status, 400)
                    self.assertTrue(self.journal.alerts_enabled)
                    client.request("POST", "/api/alerts", body, headers)
                    self.assertEqual(client.getresponse().status, 204)
                    client.request("GET", "/api/status")
                    self.assertFalse(json.load(client.getresponse())["alerts_enabled"])
                    for seconds in (10, 30, 60, 300):
                        client.request("POST", "/api/interval", json.dumps({"interval_seconds": seconds}), headers)
                        self.assertEqual(client.getresponse().status, 204)
                        client.request("GET", "/api/status")
                        self.assertEqual(json.load(client.getresponse())["speed"]["interval_seconds"], seconds)
                    for invalid in (0, 11, True, "10", 10.0):
                        client.request("POST", "/api/interval", json.dumps({"interval_seconds": invalid}), headers)
                        self.assertEqual(client.getresponse().status, 400)
                    restarted = Journal(self.path)
                    self.assertEqual(restarted.interval_seconds, 300)
                    self.assertFalse(restarted.alerts_enabled)
            finally:
                server.shutdown()

    def test_dashboard_handles_client_disconnects_but_not_other_write_errors(self):
        with DashboardServer(("127.0.0.1", 0), Dashboard) as server:
            server.page = b"dashboard"
            request = f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n\r\n".encode("ascii")

            def interrupt_body(error):
                client = Mock()
                client.makefile.return_value = BytesIO(request)
                client.sendall.side_effect = [None, error]  # Headers succeed, body write fails.
                Dashboard(client, ("127.0.0.1", 12345), server)

            for error in (ct.WinError(10053), ConnectionResetError(), BrokenPipeError()):
                with self.subTest(error=type(error).__name__), patch.object(Dashboard, "log_message") as log:
                    interrupt_body(error)
                    log.assert_called_once()
            with self.assertRaisesRegex(OSError, "unexpected write failure"):
                interrupt_body(OSError("unexpected write failure"))

    def test_interval_changes_reschedule_a_sleeping_worker_from_the_last_start(self):
        journal = self.journal
        self.assertEqual(journal.snapshot()["speed"]["interval_seconds"], 10)
        journal.observe(sample("connected", 0))
        journal.set_interval_seconds(30)
        timeouts, result = Queue(), Queue()
        real_wait = journal.lock.wait

        def wait(seconds):
            timeouts.put(seconds)
            return real_wait(seconds)

        last_start = time.monotonic() - 20
        with patch.object(journal.lock, "wait", side_effect=wait):
            worker = Thread(target=lambda: result.put(journal.wait_for_speed_test(last_start)), daemon=True)
            worker.start()
            try:
                self.assertAlmostEqual(timeouts.get(timeout=3), 10, delta=2)
                journal.set_interval_seconds(300)
                self.assertAlmostEqual(timeouts.get(timeout=3), 280, delta=2)
                journal.set_interval_seconds(10)
                self.assertEqual(result.get(timeout=3), journal.connection_epoch)
            finally:
                journal.stop()
                worker.join(timeout=3)

    def test_legacy_graph_readings_keep_their_interval_after_upgrade(self):
        path = self.path.with_name("legacy.sqlite3")
        now = time.time_ns() // 1_000_000
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE readings (at_ms INTEGER NOT NULL, mbps REAL, new_segment INTEGER NOT NULL)")
            db.execute("INSERT INTO readings VALUES (?, 4, 0)", (now - 30_000,))
        store = LocalStore(path)
        store.record(now, 3.5, True, 10)
        self.assertEqual([(p["mbps"], p["interval_seconds"]) for p in store.history(15)["samples"]],
                         [(4, 30), (3.5, 10)])

    def test_measured_rate_uses_received_bits_and_rejects_invalid_responses(self):
        response = MagicMock(status=206, headers={"Content-Range": "bytes 0-524287/104857600"})
        response.__enter__.return_value = response
        response.read1.side_effect = BytesIO(bytes(524288)).read1
        with patch.object(download, "urlopen", return_value=response) as opened:
            with patch.object(download.time, "monotonic", side_effect=[i / 4 for i in range(17)]):
                self.assertEqual(download.measure_download(), 1.048576)
            self.assertEqual(opened.call_args.args[0].get_header("Range"), "bytes=0-524287")
            with self.assertRaisesRegex(OSError, "ended before"):
                download.measure_download()
            response.status = 200
            with self.assertRaisesRegex(OSError, "requested uncompressed byte range"):
                download.measure_download()

    def test_native_notifications_track_adapters_and_ignore_failed_reconnects(self):
        # Drive the registered native callback without changing the machine's Wi-Fi.
        api = Mock()
        api.WlanOpenHandle.return_value = api.WlanCloseHandle.return_value = 0
        api.WlanRegisterNotification.return_value = 0
        first, second = wlan.Guid(1), wlan.Guid(2)
        interfaces = wlan.InterfaceList(1, 0, wlan.Interface(first, "", 1))

        def enumerate_interfaces(_handle, _reserved, output):
            ct.cast(output, ct.POINTER(ct.POINTER(wlan.InterfaceList)))[0] = ct.pointer(interfaces)
            return 0

        api.WlanEnumInterfaces.side_effect = enumerate_interfaces
        with patch.object(ct, "WinDLL", return_value=api), closing(wlan._session()) as source:
            self.assertEqual(next(source).state, "connected")
            callback = api.WlanRegisterNotification.call_args.args[3]

            def emit(guid, code, reason=0):
                result = wlan.ConnectionResult(reason=reason)
                event = wlan.Notification(wlan.ACM, code, guid, ct.sizeof(result), ct.addressof(result))
                callback(ct.pointer(event), None)

            emit(second, wlan.CONNECTION_COMPLETE)
            emit(first, wlan.INTERFACE_REMOVAL)
            emit(second, wlan.DISCONNECTED)
            emit(second, wlan.CONNECTION_COMPLETE, reason=1)
            emit(second, wlan.DISCONNECTED)
            emit(second, wlan.CONNECTION_COMPLETE)
            self.assertEqual([next(source).state for _ in range(5)], [
                "connected", "connected", "disconnected", "disconnected", "connected"])
        api.WlanFreeMemory.assert_called_once()
        api.WlanCloseHandle.assert_called_once()
