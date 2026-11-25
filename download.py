import time
from urllib.request import Request, urlopen


INTERVAL_OPTIONS = (10, 30, 60, 300)
DEFAULT_INTERVAL_SECONDS = 10
THRESHOLD_MBPS = 1.0
TEST_BYTES = 512 * 1024
TEST_URL = "https://fsn1-speed.hetzner.com/100MB.bin"


def measure_download() -> float:
    end = TEST_BYTES - 1
    request = Request(TEST_URL, headers={"Range": f"bytes=0-{end}",
                                        "Accept-Encoding": "identity", "Cache-Control": "no-cache"})
    with urlopen(request, timeout=8) as response:
        if (response.status != 206 or
                response.headers.get("Content-Range", "").partition("/")[0] != f"bytes 0-{end}" or
                response.headers.get("Content-Encoding", "identity").lower() != "identity"):
            raise OSError("Test server did not return the requested uncompressed byte range.")

        # Time the body transfer, not DNS/TLS setup. Sample partial progress on very slow links.
        started = time.monotonic()
        received = 0
        while received < TEST_BYTES:
            chunk = response.read1(min(32768, TEST_BYTES - received))
            if not chunk:
                raise OSError("Test download ended before the requested range was received.")
            received += len(chunk)
            elapsed = time.monotonic() - started
            if elapsed >= 8:
                break
        if elapsed <= 0:
            raise OSError("Test download was too short to time.")
        return received * 8 / elapsed / 1_000_000
