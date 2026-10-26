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
3850 mV. Commands used here are 1 (identity), 3 (link up), 4 (battery) and
8 (read-only EEPROM, for the current DPI gear and its count).
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
CMD_EEPROM = 0x08
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
    dpi: int | None = None
    dpi_value: int | None = None


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


def build_eeprom_frame(address: int = 0, length: int = 10) -> bytes:
    """Read-only Compx EEPROM query. Address is big-endian, matching the hub."""
    frame = bytearray(REPORT_LEN)
    frame[0] = REPORT_ID
    frame[1] = CMD_EEPROM
    frame[3] = (address >> 8) & 0xFF
    frame[4] = address & 0xFF
    frame[5] = length & 0xFF
    frame[16] = checksum(frame)
    return bytes(frame)


# Compx hub ``Tt`` map. CurrentDPI is a 0-based index. Each DPI slot is 4 bytes.
EEPROM_CURRENT_DPI = 4
EEPROM_DPI_VALUE = 12


def query_dpi(io: HidIo, log=None, timeout: float = 0.7) -> int | None:
    """Current DPI gear, 1 through 8. None when the mouse does not answer.

    The hub reads EEPROM address 4, length 2 (``Tt.CurrentDPI``). The index
    is 0-based and the next byte is ``0x55 - index``. Gear 1 is the first slot.
    """
    frame = build_eeprom_frame(EEPROM_CURRENT_DPI, 2)
    if log:
        log("TX dpi " + frame.hex())
    packet, _saw = _transact(io, frame, timeout, lambda _packet: True)
    if log:
        log("RX dpi " + (packet.hex() if packet else "none"))
    if packet is None or packet[2] != 0:
        return None
    level = packet[6]
    if not 0 <= level <= 7 or packet[7] != ((0x55 - level) & 0xFF):
        return None
    return level + 1


def _dpi_number(x_byte: int, y_byte: int, extra: int, sensor: str = "PAW3395") -> int | None:
    """Hub decode for the X value of one DPI slot.

    Delux PAW3395 / PAW3950 follow ``Ki()``: ``(code + 1) * 50``, then doubled
    for each low flag bit. The high gear (above 26000 on this M900) sets bit 0.
    PAW3950Ultra is the ATK scale, not the Delux one.
    """
    x_code = x_byte | (((extra & 0x0C) >> 2) << 8)
    if sensor == "PAW3950Ultra":
        value = x_code * 50 + 10050 if extra & 0x02 else (x_code + 1) * 10
        if extra & 0x01:
            value *= 2
        cap = 60000
    else:
        value = (x_code + 1) * 50
        if extra & 0x01:
            value *= 2
        if extra & 0x02:
            value *= 2
        cap = 60000 if "3950" in sensor else 52000
    if 50 <= value <= cap:
        return value
    return None


def dpi_value_for(io: HidIo, gear: int, timeout: float = 0.04, sensor: str = "PAW3395") -> int | None:
    """DPI count for a 1-based gear. Slot ``n`` is 4 bytes at EEPROM ``12 + 4n``."""
    if not 1 <= gear <= 8:
        return None
    frame = build_eeprom_frame(EEPROM_DPI_VALUE + (gear - 1) * 4, 4)
    packet, _saw = _transact(io, frame, timeout, lambda _packet: True)
    if packet is None or packet[2] != 0 or len(packet) < 10:
        return None
    x_byte, y_byte, extra, crc = packet[6:10]
    if crc != ((0x55 - ((x_byte + y_byte + extra) & 0xFF)) & 0xFF):
        return None
    return _dpi_number(x_byte, y_byte, extra, sensor)


def sensor_for(cid: int | None, mid: int | None) -> str:
    profile = PROFILES.get((cid, mid)) if cid is not None and mid is not None else None
    if profile is None:
        return "PAW3395"
    return profile[1]


def read_dpi(io: HidIo, log=None, timeout: float = 0.18, sensor: str = "PAW3395") -> tuple[int | None, int | None]:
    """``(gear, dpi_count)`` while the mouse is already awake. Either may be missing."""
    gear = query_dpi(io, log, timeout=timeout)
    if not gear:
        return None, None
    return gear, dpi_value_for(io, gear, timeout=timeout, sensor=sensor)


def peek_dpi_for(receivers: list[HidDevice], name: str, lock_name: str, timeout: float = 0.04) -> Reading | None:
    """One short DPI read. Skips the device when a battery read already holds it."""
    if not receivers:
        return None
    mutex = kernel32.CreateMutexW(None, False, lock_name)
    if not mutex:
        return None
    waited = kernel32.WaitForSingleObject(mutex, 0)
    if waited not in (0, 0x80):
        kernel32.CloseHandle(mutex)
        return None
    dev = receivers[0]
    try:
        with HidIo(dev.path, dev.input_len or REPORT_LEN) as io:
            gear, value = read_dpi(io, timeout=timeout)
        if not gear:
            return None
        return Reading(
            True, name, None, None, None, "", f"2.4G · {dev.vid:04X}:{dev.pid:04X}",
            dev.vid, dev.pid, None, None, gear, value,
        )
    except OSError:
        return None
    finally:
        kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)


def peek_dpi() -> Reading | None:
    return peek_dpi_for(find_receivers(), "多彩无线鼠标", HID_LOCK)


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
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        packet = io.read(max(1, min(30, int(remain * 1000))))
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
    waited = kernel32.WaitForSingleObject(mutex, 500)
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
                    # An asleep mouse answers at once with byte 6 clear. Stop there
                    # instead of polling until the deadline.
                    online, saw_online = _transact(io, online_frame, 0.45, lambda packet: True)
                    log("RX online " + (online.hex() if online else "none"))
                    if online is None or online[6] != 1:
                        detail = "没有回复，动一下鼠标" if not saw_online else "鼠标休眠或没连上，动一下鼠标"
                        last = Reading(False, "多彩 M900 Mini Pro+", None, None, None, detail, source, dev.vid, dev.pid)
                        continue
                    log("TX battery " + battery_frame.hex())
                    battery, _saw_battery = _transact(io, battery_frame, 0.6, lambda packet: packet[6] <= 100)
                    log("RX battery " + (battery.hex() if battery else "none"))
                    if battery is None:
                        last = Reading(False, "多彩 M900 Mini Pro+", None, None, None, "在线但没有电量回包", source, dev.vid, dev.pid)
                        continue
                    level = battery[6]
                    if level > 100:
                        last = Reading(False, "多彩 M900 Mini Pro+", None, None, None, f"电量字节异常：{level}", source, dev.vid, dev.pid)
                        continue
                    charging = battery[7] == 1
                    log("TX info " + info_frame.hex())
                    info, _saw_info = _transact(io, info_frame, 0.35, lambda packet: True)
                    log("RX info " + (info.hex() if info else "none"))
                    cid = info[10] if info else None
                    mid = info[11] if info else None
                    name, _chips = _profile_name(cid, mid)
                    gear, dpi_value = read_dpi(io, log, timeout=0.18, sensor=sensor_for(cid, mid))
                    detail = "充电中" if charging else "使用电池"
                    return Reading(
                        True, name, level, charging, None, detail, source,
                        dev.vid, dev.pid, cid, mid, gear, dpi_value,
                    )
            except OSError as exc:
                last = Reading(False, "多彩 M900 Mini Pro+", None, None, None, f"读取失败：{exc}", source, dev.vid, dev.pid)
                log(str(exc))
        return last

    return _locked(once)


def read_batteries(debug=None) -> list[Reading]:
    if not find_receivers():
        return []
    return [read_battery(debug)]


def format_line(reading: Reading) -> str:
    if reading.ok and reading.level is not None:
        return f"{reading.name}  {reading.level}%  {reading.detail}"
    return f"{reading.name}  --  {reading.detail}"
