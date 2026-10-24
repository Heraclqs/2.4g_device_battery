"""Delux / Compx 2.4G battery query.

Verified on the receiver plugged into this machine:

* product string ``DELUX-1K Dongle``, manufacturer ``Compx``
* VID ``0x3554``, PID ``0xFB23`` (the web driver also lists ``FB26`` / wired ``FB25``)
* collection usage page ``0xFF02``, usage ``0x0002``, 17-byte input and output
* device info command returned cid 93, mid 1, type 0 (wireless 1 kHz)
* that profile in Delux's own config is PAW3395 + NRF52833

The same framing is what the official Compx web driver builds. Report id is
``0x08``. On the wire the checksum is ``(0x55 - sum(first 16 bytes)) & 0xFF``.

Battery reply, indexes on the 17-byte Windows buffer (report id included):

* ``[1]`` command ``0x04``, ``[2]`` status ``0``
* ``[6]`` percent, ``[7]`` 1 while charging, ``[8:10]`` voltage in mV, big-endian

A live reply was ``08 04 00 00 00 02 2D 00 0F 0A ... 01``: 45%, not charging,
3850 mV. Only commands 1 (identity), 3 (link up) and 4 (battery) are sent.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass

from hid_win import HidDevice, HidIo, enumerate_hid, kernel32

REPORT_ID = 0x08
CMD_INFO = 0x01
CMD_ONLINE = 0x03
CMD_BATTERY = 0x04
REPORT_LEN = 17

# Official hub config for VID 0x3554. FB23 is the 1 kHz dongle on this PC.
PIDS = {0xFB23, 0xFB26, 0xFB25}
USAGE_PAGE = 0xFF02
USAGE = 0x0002

# cid 93 is the only mouse family in the current Delux hub config.
# mid 1 is the unit that answered here (PAW3395 / NRF52833).
PROFILES = {
    (93, 1): ("多彩 M900 Mini Pro+", "PAW3395", "NRF52833"),
    (93, 3): ("多彩 M900 Mini Pro+", "PAW3395", "NRF52840"),
    (93, 5): ("多彩 M900 Mini Pro+", "PAW3395", "NRF52840"),
    (93, 2): ("多彩 M900 Ultra", "PAW3950", "NRF52840"),
    (93, 4): ("多彩 M900 Ultra", "PAW3950", "NRF52840"),
    (93, 6): ("多彩 M900 Ultra", "PAW3950", "NRF52840"),
}

HID_LOCK = "Local\\Delux24GBatteryHid"
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
kernel32.ReleaseMutex.argtypes = [ctypes.wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]


@dataclass
class Reading:
    ok: bool
    name: str
    level: int | None
    charging: bool | None
    voltage_mv: int | None
    detail: str
    source: str
    vid: int = 0
    pid: int = 0
    cid: int | None = None
    mid: int | None = None


def level_rgb(level: int | None) -> tuple[int, int, int]:
    if level is None:
        return (150, 155, 160)
    if level >= 60:
        return (93, 220, 138)
    if level >= 30:
        return (245, 196, 81)
    return (255, 107, 107)


def level_hex(level: int | None) -> str:
    r, g, b = level_rgb(level)
    return f"#{r:02x}{g:02x}{b:02x}"


def checksum(frame: bytes | bytearray) -> int:
    return (0x55 - (sum(frame[:16]) & 0xFF)) & 0xFF


def build_frame(command: int, payload: bytes = b"") -> bytes:
    frame = bytearray(REPORT_LEN)
    frame[0] = REPORT_ID
    frame[1] = command & 0xFF
    if payload:
        frame[5] = len(payload) & 0xFF
        frame[6:6 + len(payload)] = payload[:10]
    frame[16] = checksum(frame)
    return bytes(frame)


def _valid(packet: bytes | None, command: int) -> bool:
    if packet is None or len(packet) < REPORT_LEN:
        return False
    if packet[0] != REPORT_ID or packet[1] != command or packet[2] != 0:
        return False
    return packet[16] == checksum(packet)


def _transact(io: HidIo, frame: bytes, timeout_s: float, accept) -> tuple[bytes | None, bool]:
    """Resend until ``accept`` matches a valid echo. The bool is whether any echo arrived."""
    deadline = time.monotonic() + timeout_s
    next_send = 0.0
    saw = False
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_send:
            io.write(frame)
            next_send = now + 0.28
        packet = io.read(120)
        if not _valid(packet, frame[1]):
            continue
        saw = True
        if accept(packet):
            return packet, True
    return None, saw


def find_receivers() -> list[HidDevice]:
    devices = []
    for dev in enumerate_hid():
        if dev.vid != 0x3554 or dev.pid not in PIDS:
            continue
        if dev.usage_page == USAGE_PAGE and dev.usage == USAGE and dev.output_len == REPORT_LEN:
            devices.append(dev)
    return devices


def _profile_name(cid: int | None, mid: int | None) -> tuple[str, str]:
    if cid is None or mid is None:
        return "多彩无线鼠标", ""
    profile = PROFILES.get((cid, mid))
    if profile is None:
        return f"多彩鼠标 cid {cid} mid {mid}", ""
    name, sensor, mcu = profile
    return name, f"{sensor} · {mcu}"


def _locked(fn):
    mutex = kernel32.CreateMutexW(None, False, HID_LOCK)
    if not mutex:
        return fn()
    waited = kernel32.WaitForSingleObject(mutex, 4000)
    if waited not in (0, 0x80):  # WAIT_OBJECT_0 or WAIT_ABANDONED
        kernel32.CloseHandle(mutex)
        return Reading(False, "多彩无线鼠标", None, None, None, "另一个程序正在读电量", "")
    try:
        return fn()
    finally:
        kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)


def read_battery(debug=None) -> Reading:
    """Query the first Delux receiver that answers. ``debug`` receives hex lines."""

    def log(text: str) -> None:
        if debug:
            debug(text)

    receivers = find_receivers()
    if not receivers:
        return Reading(False, "多彩 M900 Mini Pro+", None, None, None, "没找到接收器", "VID 3554")

    def once() -> Reading:
        last = Reading(False, "多彩 M900 Mini Pro+", None, None, None, "没有回复，动一下鼠标", "")
        for dev in receivers:
            source = f"2.4G · {dev.vid:04X}:{dev.pid:04X} · {dev.product or 'DELUX'}"
            try:
                with HidIo(dev.path, dev.input_len or REPORT_LEN) as io:
                    # Drop a stale report if one is already sitting in the buffer.
                    stale = io.read(40)
                    if stale:
                        log("stale " + stale.hex())
                    info_frame = build_frame(CMD_INFO, bytes([0x11, 0x22, 0x33, 0x44, 0, 0, 0, 0]))
                    online_frame = build_frame(CMD_ONLINE)
                    battery_frame = build_frame(CMD_BATTERY)
                    log("TX online " + online_frame.hex())
                    # A sleeping mouse answers "offline" at once. Keep polling so the
                    # receiver's own radio wakeup has a chance before we give up.
                    online, saw_online = _transact(io, online_frame, 2.2, lambda packet: packet[6] == 1)
                    log("RX online " + (online.hex() if online else "none"))
                    if online is None:
                        detail = "没有回复，动一下鼠标" if not saw_online else "鼠标休眠或没连上，动一下鼠标"
                        last = Reading(False, "多彩 M900 Mini Pro+", None, None, None, detail, source, dev.vid, dev.pid)
                        continue
                    log("TX battery " + battery_frame.hex())
                    battery, _saw_battery = _transact(io, battery_frame, 1.2, lambda packet: packet[6] <= 100)
                    log("RX battery " + (battery.hex() if battery else "none"))
                    if battery is None:
                        last = Reading(False, "多彩 M900 Mini Pro+", None, None, None, "在线但没有电量回包", source, dev.vid, dev.pid)
                        continue
                    level = battery[6]
                    if level > 100:
                        last = Reading(False, "多彩 M900 Mini Pro+", None, None, None, f"电量字节异常：{level}", source, dev.vid, dev.pid)
                        continue
                    charging = battery[7] == 1
                    voltage = (battery[8] << 8) | battery[9]
                    log("TX info " + info_frame.hex())
                    info, _saw_info = _transact(io, info_frame, 0.8, lambda packet: True)
                    log("RX info " + (info.hex() if info else "none"))
                    cid = info[10] if info else None
                    mid = info[11] if info else None
                    name, chips = _profile_name(cid, mid)
                    detail = "充电中" if charging else "使用电池"
                    if chips:
                        detail = f"{detail} · {chips}"
                    if voltage:
                        detail = f"{detail} · {voltage / 1000:.3f} V"
                    return Reading(
                        True, name, level, charging, voltage or None, detail, source,
                        dev.vid, dev.pid, cid, mid,
                    )
            except OSError as exc:
                last = Reading(False, "多彩 M900 Mini Pro+", None, None, None, f"读取失败：{exc}", source, dev.vid, dev.pid)
                log(str(exc))
        return last

    return _locked(once)


def format_line(reading: Reading) -> str:
    if reading.ok and reading.level is not None:
        return f"{reading.name}  {reading.level}%  {reading.detail}"
    return f"{reading.name}  --  {reading.detail}"
