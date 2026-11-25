# Wi-Fi disruption monitor

A manual-start Windows monitor for the **Wi-Fi link to an access point** and **sampled internet download speed**. It listens to WLAN AutoConfig connection events and rechecks adapter state every two idle seconds. A separate worker tests download speed without blocking disconnect detection. It doesn't collect network names or install a background service.

## Run

With Python 3.10 or newer installed, open a terminal in this folder and run:

```powershell
python .\monitor.py
```

Open **<http://127.0.0.1:8765/>** for the live status and all recorded disruptions. Keep the terminal running; the dashboard tab can be closed. On each new disconnect, the monitor requests a new tab in your default browser and the page displays a dismissible alert. The browser controls whether it uses a tab or a window. The page works without Wi-Fi and refreshes every two seconds.

Press **Ctrl+C** to stop. Run the same command later to resume. Nothing is scheduled to run at sign-in.

## What gets recorded

- History is saved to **`%LOCALAPPDATA%\NetworkMonitor\disruptions.json`** as a JSON array. The file is replaced atomically when an outage starts or ends. Local times in the dashboard are converted from the UTC timestamps in the file.
- A drop starts when the last connected Wi-Fi adapter disconnects or disappears. Repeated notifications do not create extra rows or tabs. The first connection observation is a baseline: starting the monitor already offline does not invent a disconnect time.
- `disconnected_at` is saved immediately. On recovery, the same record gets `reconnected_at`, `duration_seconds` (measured with a monotonic clock), and `status: "restored"`. An ongoing record has null end and duration fields; the page displays its elapsed time live.
- If observation ends during an outage, the record is marked `interrupted` instead of claiming a recovery. A known partial duration is shown as a lower bound; after an abrupt exit the duration may be unknown. Events while the monitor is not running are not recorded.
- WLAN service errors are shown separately from disconnects. The monitor retries automatically. Only the loopback interface serves the dashboard.

## Download-speed checks

The first test starts when Wi-Fi is connected, then **every 10 seconds by default** while connected. Use **Speed test interval** on the dashboard to select **10, 30, 60, or 300 seconds**. The choice is saved on this PC. Changes reschedule the next check relative to the last test's start, without restarting the monitor or interrupting a test already in progress. Tests never overlap.

Each test requests up to 512 KiB from [Hetzner's public speed-test server](https://fsn1-speed.hetzner.com/). At 10 seconds, this uses up to **180 MiB per hour**, plus network overhead (60 MiB/hour at 30 seconds, 30 at 60, or 6 at 300). Only test data is downloaded; local history is not uploaded. The test uses Windows' normal internet route, which may include Ethernet or a VPN.

The dashboard shows the latest measured rate in **Mbps** (1 Mbps = 1,000,000 bits/second) and its timestamp. This is an estimate to one server, not the Wi-Fi link rate or the maximum speed of your plan. It measures received body bytes over transfer time, excluding connection setup. Very slow transfers can use a partial sample.

A successful sample **below 1 Mbps** is logged to **`%LOCALAPPDATA%\NetworkMonitor\speed-alerts.json`** with its UTC timestamp and measured speed. The monitor opens one alert tab, not one per failing sample. A sample **at or above 1 Mbps** re-arms the alert. A new monitor session starts with a fresh threshold check. Test errors are shown as unavailable, not recorded as zero speed or as Wi-Fi disconnects. A result spanning a Wi-Fi disconnect is discarded. The dashboard and disconnect monitoring still work offline.

## Speed graph and alert preference

**Download speed over time** plots the existing tests, with selectable **15-minute, 1-hour (default), 6-hour, and 24-hour** windows. The horizontal axis is local time; the dashed line marks 1 Mbps. Failed tests, connection changes, restarts, and long gaps break the line rather than implying zero speed. Changing the timeframe does not run a new test.

Graph readings are stored separately in `%LOCALAPPDATA%\NetworkMonitor\monitor.sqlite3`. The graph starts collecting on the next run; old alert entries are not a complete speed history. Readings older than 24 hours are removed on startup and when a new result is recorded. The JSON alert and disconnect logs are unchanged.

To mute browser alerts, uncheck **Open alert tabs for disconnects and slow speeds**. Both alert types still get logged, and the graph keeps updating. The setting is on by default and saved on this PC, even if you close the dashboard or restart the monitor. Re-enabling it applies to new events; it does not replay muted alerts.

Run the checks with `python -m unittest -v`.
