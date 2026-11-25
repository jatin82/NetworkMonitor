"""Read Windows WLAN AutoConfig events without querying SSIDs or pinging the internet."""

import ctypes as ct
from ctypes import wintypes as wt
from dataclasses import dataclass, field
from datetime import datetime, timezone
from queue import Empty, Queue
import time
from typing import Iterator, Literal


Link = Literal["connected", "disconnected", "no-adapter", "unavailable"]
ACM = 0x08
CONNECTION_COMPLETE, INTERFACE_REMOVAL, DISCONNECTED = 10, 14, 21
Guid = wt.DWORD * 4


@dataclass(frozen=True)
class Sample:
    state: Link
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tick: float = field(default_factory=time.monotonic)
    error: str | None = None


class Interface(ct.Structure):
    _fields_ = [("guid", Guid), ("description", wt.WCHAR * 256), ("state", wt.DWORD)]


class InterfaceList(ct.Structure):
    _fields_ = [("count", wt.DWORD), ("index", wt.DWORD), ("first", Interface)]


class Notification(ct.Structure):
    _fields_ = [("source", wt.DWORD), ("code", wt.DWORD), ("guid", Guid),
                ("size", wt.DWORD), ("data", ct.c_void_p)]


class ConnectionResult(ct.Structure):
    # Prefix of WLAN_CONNECTION_NOTIFICATION_DATA through wlanReasonCode.
    _fields_ = [("mode", wt.DWORD), ("profile", wt.WCHAR * 256),
                ("ssid_length", wt.DWORD), ("ssid", ct.c_ubyte * 32),
                ("bss_type", wt.DWORD), ("secure", wt.BOOL), ("reason", wt.DWORD)]


def _check(code: int) -> None:
    if code:
        raise ct.WinError(code)


def _state(adapters: dict[bytes, bool]) -> Link:
    if not adapters:
        return "no-adapter"
    return "connected" if any(adapters.values()) else "disconnected"


def _session() -> Iterator[Sample]:
    callback_type = ct.WINFUNCTYPE(None, ct.POINTER(Notification), ct.c_void_p)
    api = ct.WinDLL("wlanapi.dll", winmode=0x800)  # LOAD_LIBRARY_SEARCH_SYSTEM32
    ptr = ct.c_void_p
    handle = wt.HANDLE()
    api.WlanOpenHandle.argtypes = [wt.DWORD, ptr, ct.POINTER(wt.DWORD), ct.POINTER(wt.HANDLE)]
    api.WlanCloseHandle.argtypes = [wt.HANDLE, ptr]
    api.WlanEnumInterfaces.argtypes = [wt.HANDLE, ptr, ct.POINTER(ct.POINTER(InterfaceList))]
    api.WlanFreeMemory.argtypes = [ptr]
    api.WlanFreeMemory.restype = None
    api.WlanRegisterNotification.argtypes = [wt.HANDLE, wt.DWORD, wt.BOOL, callback_type,
                                             ptr, ptr, ct.POINTER(wt.DWORD)]
    events: Queue[tuple[bytes, bool | None, datetime, float]] = Queue()

    @callback_type
    def on_event(pointer, _context):
        event = pointer.contents
        if event.source != ACM:
            return
        if event.code == CONNECTION_COMPLETE:
            if not event.data or event.size < ct.sizeof(ConnectionResult):
                return
            if ConnectionResult.from_address(event.data).reason != 0:
                return  # A failed connection attempt is not a recovery.
            connected = True
        elif event.code == DISCONNECTED:
            connected = False
        elif event.code == INTERFACE_REMOVAL:
            connected = None
        else:
            return
        events.put((bytes(event.guid), connected, datetime.now(timezone.utc), time.monotonic()))

    def snapshot() -> dict[bytes, bool]:
        result = ct.POINTER(InterfaceList)()
        _check(api.WlanEnumInterfaces(handle, None, ct.byref(result)))
        try:
            data = result.contents
            items = (Interface * data.count).from_address(
                ct.addressof(data) + InterfaceList.first.offset)
            return {bytes(item.guid): item.state == 1 for item in items}
        finally:
            api.WlanFreeMemory(result)

    _check(api.WlanOpenHandle(2, None, ct.byref(wt.DWORD()), ct.byref(handle)))
    try:
        _check(api.WlanRegisterNotification(handle, ACM, False, on_event, None, None, None))
        adapters = snapshot()
        yield Sample(_state(adapters))
        while True:
            try:
                guid, connected, at, tick = events.get(timeout=2)
            except Empty:
                # Also reconcile driver changes and missed notifications.
                adapters = snapshot()
                yield Sample(_state(adapters))
            else:
                if connected is None:
                    adapters.pop(guid, None)
                else:
                    adapters[guid] = connected
                yield Sample(_state(adapters), at, tick)
    finally:
        _check(api.WlanCloseHandle(handle, None))


def samples() -> Iterator[Sample]:
    while True:
        try:
            yield from _session()
        except OSError as exc:
            yield Sample("unavailable", error=str(exc))
            time.sleep(3)
