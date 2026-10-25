"""AULA L3 battery query.

The official hub (hub.aulacn.com, layout W9 / xxwd) and this machine's receiver
agree on the same 64-byte frame. Windows adds a zero report id, so the
WriteFile / ReadFile buffer is 65 bytes.

Verified on the receiver plugged into this machine:

* product ``USB Receiver``, manufacturer ``YJX-CHIP``
* VID ``0xA8A5``, PID ``0x2255`` (wired mouse is VID ``0xA8A4``, same PID)
* collection usage page ``0xFF01``, usage ``0x0010``
* input 65, output 65, report id 0

A live battery reply was ``00 AA 30 01 2E 0A 00 00 00 5E ...``: 94%, not charging.
"""

from __future__ import annotations

import ctypes
import time

from delux_battery import Reading
from hid_win import HidDevice, HidIo, enumerate_hid, kernel32

FRAME_LEN = 64
WIRE_LEN = 65
REPORT_ID = 0x00
FLAG_OUT = 0x55
FLAG_IN = 0xAA
DEV_MOUSE = 0x01
CMD_BATTERY = 0x30
CMD_PERFORMANCE = 0x0E
BATTERY_LEN = 0x2E
PERF_LEN = 16
HEADER = 8
# Performance block, from the hub's xxwd map: count at 3, current gear at 4,
# then little-endian DPI values starting at 5.

# Hub catalog: L3 wired mouse and its 2.4G receiver share PID 0x2255.
PIDS = {0x2255}
VIDS = {0xA8A4, 0xA8A5}
USAGE_PAGE = 0xFF01
USAGE = 0x0010

HID_LOCK = "Local\\AulaL3BatteryHid"
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
kernel32.ReleaseMutex.argtypes = [ctypes.wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]


def build_frame(command: int, length: int = 0, payload: bytes = b"", addr: int = 0) -> bytes:
    frame = bytearray(FRAME_LEN)
    frame[0] = FLAG_OUT
    frame[1] = command & 0xFF
    frame[2] = DEV_MOUSE
    frame[4] = (length or len(payload)) & 0xFF
    frame[5] = addr & 0xFF
    frame[6] = (addr >> 8) & 0xFF
    frame[8:8 + len(payload)] = payload[: FRAME_LEN - HEADER]
    end = min(FRAME_LEN, HEADER + frame[4])
    frame[3] = sum(frame[4:end]) & 0xFF
    return bytes(frame)


def _body(packet: bytes) -> bytes | None:
    if not packet:
        return None
    if packet[0] == FLAG_IN:
        return packet
    if len(packet) > 1 and packet[1] == FLAG_IN:
        return packet[1:]
    return None


def find_receivers() -> list[HidDevice]:
    devices = []
    for dev in enumerate_hid():
        if dev.vid not in VIDS or dev.pid not in PIDS:
            continue
        if dev.usage_page == USAGE_PAGE and dev.usage == USAGE and dev.output_len == WIRE_LEN:
            devices.append(dev)
    return devices


def _locked(fn):
    mutex = kernel32.CreateMutexW(None, False, HID_LOCK)
    if not mutex:
        return fn()
    waited = kernel32.WaitForSingleObject(mutex, 500)
    if waited not in (0, 0x80):
        kernel32.CloseHandle(mutex)
        return [Reading(False, "狼蛛 L3", None, None, None, "另一个程序正在读电量", "")]
    try:
        return fn()
    finally:
        kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)


def _query_dpi(io: HidIo, log, timeout: float = 0.7) -> tuple[int | None, int | None]:
    """Return ``(gear, dpi_value)``. Gear is 1-based, matching the hub byte."""
    packet = bytes([REPORT_ID]) + build_frame(CMD_PERFORMANCE, PERF_LEN)
    if log:
        log("TX dpi " + packet[:12].hex())
    deadline = time.monotonic() + timeout
    next_send = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_send:
            io.write(packet)
            next_send = now + 0.28
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        reply = io.read(max(1, min(30, int(remain * 1000))))
        if not reply:
            continue
        body = _body(reply)
        if body is None or len(body) < HEADER + 5 or body[1] != CMD_PERFORMANCE:
            continue
        if log:
            log("RX dpi " + reply[:24].hex())
        data = body[HEADER:]
        count = data[3]
        gear = data[4]
        if not 1 <= gear <= 8:
            return None, None
        if 1 <= count <= 8 and gear > count:
            return None, None
        value = None
        value_at = 5 + (gear - 1) * 2
        if value_at + 1 < len(data):
            raw = data[value_at] | (data[value_at + 1] << 8)
            if 50 <= raw <= 30000:
                value = raw
        return gear, value
    if log:
        log("RX dpi none")
    return None, None


def peek_dpi() -> Reading | None:
    """Short gear read for the live DPI watcher. None when the mouse is busy or asleep."""
    receivers = find_receivers()
    if not receivers:
        return None
    mutex = kernel32.CreateMutexW(None, False, HID_LOCK)
    if not mutex:
        return None
    waited = kernel32.WaitForSingleObject(mutex, 0)
    if waited not in (0, 0x80):
        kernel32.CloseHandle(mutex)
        return None
    dev = receivers[0]
    try:
        with HidIo(dev.path, dev.input_len or WIRE_LEN) as io:
            gear, value = _query_dpi(io, None, timeout=0.04)
        if not gear:
            return None
        link = "有线" if dev.vid == 0xA8A4 else "2.4G"
        return Reading(
            True, "狼蛛 L3", None, None, None, "", f"{link} · {dev.vid:04X}:{dev.pid:04X}",
            dev.vid, dev.pid, dpi=gear, dpi_value=value,
        )
    except OSError:
        return None
    finally:
        kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)


def _read_one(dev: HidDevice, debug) -> Reading:
    link = "有线" if dev.vid == 0xA8A4 else "2.4G"
    source = f"{link} · {dev.vid:04X}:{dev.pid:04X} · {dev.product or 'AULA L3'}"
    frame = build_frame(CMD_BATTERY, BATTERY_LEN)
    packet = bytes([REPORT_ID]) + frame

    def log(text: str) -> None:
        if debug:
            debug(text)

    try:
        with HidIo(dev.path, dev.input_len or WIRE_LEN) as io:
            stale = io.read(40)
            if stale:
                log("stale " + stale[:16].hex())
            deadline = time.monotonic() + 0.6
            next_send = 0.0
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_send:
                    log("TX " + packet[:12].hex())
                    io.write(packet)
                    next_send = now + 0.28
                reply = io.read(40)
                if not reply:
                    continue
                log("RX " + reply[:16].hex())
                body = _body(reply)
                if body is None or len(body) < HEADER + 2 or body[1] != CMD_BATTERY:
                    continue
                level = body[8]
                if level > 100:
                    continue
                charging = body[9] != 0
                detail = "充电中" if charging else "使用电池"
                return Reading(
                    True, "狼蛛 L3", level, charging, None, detail, source,
                    dev.vid, dev.pid,
                )
    except OSError as exc:
        log(str(exc))
        return Reading(False, "狼蛛 L3", None, None, None, f"读取失败：{exc}", source, dev.vid, dev.pid)
    return Reading(False, "狼蛛 L3", None, None, None, "没有回复，动一下鼠标", source, dev.vid, dev.pid)


def read_batteries(debug=None) -> list[Reading]:
    receivers = find_receivers()
    if not receivers:
        return []

    def once() -> list[Reading]:
        return [_read_one(dev, debug) for dev in receivers]

    return _locked(once)
