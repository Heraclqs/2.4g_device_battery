"""Watch DPI gears continuously.

The HID handle stays open, so a gear change is seen in a few milliseconds
instead of paying for a fresh open on every poll.
"""

from __future__ import annotations

import base64
import ctypes
import os
import socket
import threading
import time

import alerts
import atk_mouse_battery
import aula_battery
import delux_battery
import inphic_battery
from delux_battery import Reading
from hid_win import HidIo
import win_setup as setup

LOCK_NAME = "Local\\BatteryDpiWatch"
FAST = 0.008
SLOW = 0.25
QUERY_TIMEOUT = 0.025

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
kernel32.ReleaseMutex.argtypes = [ctypes.wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]

_stop: threading.Event | None = None
_thread: threading.Thread | None = None
_enabled = True
_enabled_lock = threading.Lock()


def configure(enabled: bool) -> None:
    """Apply the DPI-alert switch. Called when the user changes settings."""
    global _enabled
    with _enabled_lock:
        _enabled = bool(enabled)


def enabled() -> bool:
    with _enabled_lock:
        return _enabled


def _aula_query(io: HidIo, dev, name: str) -> Reading | None:
    gear, value = aula_battery._query_dpi(io, None, timeout=QUERY_TIMEOUT)
    if not gear:
        return None
    link = "有线" if dev.vid == 0xA8A4 else "2.4G"
    return Reading(
        True, name, None, None, None, "", f"{link} · {dev.vid:04X}:{dev.pid:04X}",
        dev.vid, dev.pid, dpi=gear, dpi_value=value,
    )


def _compx_query(io: HidIo, dev, name: str) -> Reading | None:
    gear = delux_battery.query_dpi(io, timeout=0.12)
    if not gear:
        return None
    value = None
    try:
        sensor = "PAW3395" if dev.vid == 0x3554 else "PAW3950Ultra"
        value = delux_battery.dpi_value_for(io, gear, timeout=0.12, sensor=sensor)
    except OSError:
        value = None
    return Reading(
        True, name, None, None, None, "", f"2.4G · {dev.vid:04X}:{dev.pid:04X}",
        dev.vid, dev.pid, dpi=gear, dpi_value=value,
    )


class _Inphic:
    """The dongle pushes a DPI frame. It is not a polled EEPROM value."""

    def __init__(self, dev):
        self.dev = dev
        self.io: HidIo | None = None
        self.sock = None
        self.buf = bytearray()
        self.pending = bytearray()
        self.ws_at = 0.0
        self.last: tuple[int, int] | None = None
        self.gear: int | None = None
        self.value: int | None = None
        self.pending_gear: int | None = None
        self.pending_value: int | None = None
        self.busy = False
        self.ready = time.monotonic() + 0.4
        self.mutex = kernel32.CreateMutexW(None, False, inphic_battery.HID_LOCK)

    def close(self) -> None:
        if self.io is not None:
            self.io.close()
            self.io = None
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.mutex:
            kernel32.CloseHandle(self.mutex)
            self.mutex = None

    def _ws(self) -> tuple[int, int] | None:
        if self.sock is None:
            now = time.monotonic()
            if now - self.ws_at < 2.0:
                return None
            self.ws_at = now
            try:
                sock = socket.create_connection(("127.0.0.1", inphic_battery.HUB_PORT), 0.2)
            except OSError:
                return None
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall(
                (
                    "GET / HTTP/1.1\r\n"
                    "Host: localhost:10085\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
            )
            sock.settimeout(0.3)
            try:
                hello = sock.recv(1024)
            except OSError:
                sock.close()
                return None
            parts = hello.split(b"\r\n\r\n", 1)
            if len(parts) < 2 or b"101" not in parts[0]:
                sock.close()
                return None
            self.sock = sock
            self.pending = bytearray(parts[1])
            sock.settimeout(0.01)
        self.sock.settimeout(0.01)
        try:
            chunk = self.sock.recv(4096)
        except TimeoutError:
            chunk = None
        except OSError:
            self.sock.close()
            self.sock = None
            return None
        if chunk == b"":
            self.sock.close()
            self.sock = None
            return None
        if chunk:
            self.pending += chunk
        frames, self.pending = inphic_battery._ws_frames(self.pending)
        found = None
        for frame in frames:
            text = frame.decode("ascii", errors="ignore").strip()
            if len(text) < 4 or len(text) % 2:
                continue
            try:
                nums = [int(text[i:i + 2], 16) for i in range(0, len(text), 2)]
            except ValueError:
                continue
            inphic_battery.remember_hub(nums)
            parsed = inphic_battery.parse_dpi_frame(nums)
            if parsed:
                found = parsed
        return found

    def _hid(self) -> tuple[int, int] | None:
        if self.mutex and kernel32.WaitForSingleObject(self.mutex, 0) not in (0, 0x80):
            self.busy = True
            return None
        try:
            if self.io is None:
                self.io = HidIo(self.dev.path, self.dev.input_len or 64)
            packet = self.io.read(12)
        except OSError:
            if self.io is not None:
                self.io.close()
                self.io = None
            return None
        finally:
            if self.mutex:
                kernel32.ReleaseMutex(self.mutex)
        if not packet:
            return None
        return inphic_battery.parse_hid_dpi(packet)

    def poll(self) -> Reading | None:
        self.busy = False
        parsed = self._ws()
        if parsed is None and self.sock is None:
            parsed = self._hid()
        if parsed is None:
            return None
        device_id, gear = parsed
        return Reading(
            True, inphic_battery._model_name(device_id), None, None, None, "",
            f"2.4G · {self.dev.vid:04X}:{self.dev.pid:04X}",
            self.dev.vid, self.dev.pid,
            dpi=gear, dpi_value=inphic_battery.dpi_number(device_id, gear),
        )


class _Gear:
    def __init__(self, dev, name: str, query, lock_name: str):
        self.dev = dev
        self.name = name
        self.query = query
        self.lock_name = lock_name
        self.io: HidIo | None = None
        self.mutex = kernel32.CreateMutexW(None, False, lock_name)
        self.gear: int | None = None
        self.value: int | None = None
        self.pending: int | None = None
        self.pending_value: int | None = None
        self.busy = False

    def close(self) -> None:
        if self.io is not None:
            self.io.close()
            self.io = None
        if self.mutex:
            kernel32.CloseHandle(self.mutex)
            self.mutex = None

    def poll(self) -> Reading | None:
        if not self.mutex:
            return None
        self.busy = False
        waited = kernel32.WaitForSingleObject(self.mutex, 0)
        if waited not in (0, 0x80):
            self.busy = True
            return None
        try:
            if self.io is None:
                self.io = HidIo(self.dev.path, self.dev.input_len or 64)
            return self.query(self.io, self.dev, self.name)
        except OSError:
            self.close()
            return None
        finally:
            kernel32.ReleaseMutex(self.mutex)


def _specs() -> list[tuple]:
    found = []
    for dev in aula_battery.find_receivers():
        found.append((dev, "狼蛛 L3", _aula_query, aula_battery.HID_LOCK))
    for dev in atk_mouse_battery.find_receivers():
        found.append((dev, "ATK F1", _compx_query, atk_mouse_battery.HID_LOCK))
    for dev in delux_battery.find_receivers():
        found.append((dev, "多彩无线鼠标", _compx_query, delux_battery.HID_LOCK))
    return found


def _owned(stop: threading.Event, popup, yield_to_tray: bool) -> None:
    gears: dict[str, _Gear] = {}
    last_scan = 0.0
    awake = False
    try:
        while not stop.is_set():
            if yield_to_tray and setup.tray_hwnd():
                return
            now = time.monotonic()
            if now - last_scan > 1.0:
                last_scan = now
                listening = enabled()
                specs = _specs() if listening else []
                inphic = inphic_battery.find_receivers() if listening else []
                paths = {dev.path for dev, _name, _query, _lock in specs}
                paths.update(dev.path for dev in inphic)
                for path in list(gears):
                    if path not in paths:
                        gears.pop(path).close()
                for dev, name, query, lock in specs:
                    if dev.path not in gears:
                        gears[dev.path] = _Gear(dev, name, query, lock)
                for dev in inphic:
                    if dev.path not in gears:
                        gears[dev.path] = _Inphic(dev)
            if not enabled():
                stop.wait(SLOW)
                continue
            changed: list[Reading] = []
            awake = any(isinstance(item, _Inphic) for item in gears.values())
            busy = False
            for gear in gears.values():
                reading = gear.poll()
                if gear.busy:
                    busy = True
                if reading is None or not reading.dpi:
                    continue
                awake = True
                if isinstance(gear, _Inphic):
                    changed_gear = gear.gear != reading.dpi
                    gear.gear = reading.dpi
                    gear.value = reading.dpi_value
                    if changed_gear:
                        alerts.note_dpi(
                            [reading],
                            popup=popup,
                            announce=time.monotonic() >= gear.ready,
                        )
                    continue
                if reading.dpi == gear.pending:
                    gear.gear = reading.dpi
                    gear.value = reading.dpi_value
                    gear.pending = None
                    gear.pending_value = None
                    changed.append(reading)
                elif reading.dpi != gear.gear:
                    gear.pending = reading.dpi
                    gear.pending_value = reading.dpi_value
                else:
                    gear.pending = None
                    if reading.dpi_value != gear.value:
                        gear.value = reading.dpi_value
                        changed.append(reading)
            if changed:
                alerts.note_dpi(changed, popup=popup)
            stop.wait(FAST if awake or busy else SLOW)
    finally:
        for gear in gears.values():
            gear.close()


def _loop(stop: threading.Event, popup, yield_to_tray: bool) -> None:
    try:
        alerts.warm_toast()
    except OSError:
        pass
    mutex = kernel32.CreateMutexW(None, False, LOCK_NAME)
    if not mutex:
        return
    try:
        while not stop.is_set():
            waited = kernel32.WaitForSingleObject(mutex, 500)
            if waited not in (0, 0x80):
                continue
            try:
                _owned(stop, popup, yield_to_tray)
            finally:
                kernel32.ReleaseMutex(mutex)
    finally:
        kernel32.CloseHandle(mutex)


def start(popup=None, yield_to_tray: bool = False) -> None:
    global _stop, _thread
    if _thread is not None and _thread.is_alive():
        return
    configure(bool(setup.load_config()["dpi_alert"]))
    _stop = threading.Event()
    _thread = threading.Thread(
        target=_loop,
        args=(_stop, popup or alerts.windows_popup, yield_to_tray),
        name="dpi-watch",
        daemon=True,
    )
    _thread.start()


def stop() -> None:
    if _stop is not None:
        _stop.set()
