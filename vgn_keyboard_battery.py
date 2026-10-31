"""VGN811 battery query.

The official VGN Driver (rongyuan SDK v130) reads this keyboard with feature
command ``0x83`` (``FEA_CMD_GET_BATTERY``). Checksum type is BIT7: byte 7 of
the 64-byte payload is the low 7 bits of the sum of bytes 0..6.

Windows feature reports are 65 bytes. Byte 0 is report id ``0``. The SDK's
reply buffer is the following 64 bytes:

* ``[0]`` changes between ``0x00`` and ``0x01`` while the keyboard is awake. It is not the command echo.
* ``[1]`` percent, capped at 100
* ``[2]`` ``0`` on battery, ``1`` while charging, any other value means full

Verified on the receiver plugged into this machine:

* product ``2.4G Wireless Keyboard``, manufacturer ``ROYUAN``
* VID ``0x3151``, PID ``0x4011`` (wired in the same table is ``0x4010``)
* collection usage page ``0xFFFF``, usage ``0x0002``, feature report 65
* one feature read came back ``00 00 44 00 00 01 01 01 79 ...`` (68%)
* a later read came back ``00 01 45 00 00 01 01 01 79 ...`` (69%)

The keyboard interface disappears when the
board sleeps, so a missing device is not shown as 0%.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from delux_battery import Reading
from hid_win import GENERIC_READ, GENERIC_WRITE, HidDevice, _open, enumerate_hid, kernel32

VID = 0x3151
PIDS = {0x4011, 0x4010}
USAGE_PAGE = 0xFFFF
USAGE = 0x0002
FEATURE_LEN = 65
CMD_BATTERY = 0x83
NAME = "VGN811"

HID_LOCK = "Local\\Vgn811BatteryHid"
hid = ctypes.WinDLL("hid", use_last_error=True)
hid.HidD_SetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_ulong]
hid.HidD_SetFeature.restype = wintypes.BOOL
hid.HidD_GetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_ulong]
hid.HidD_GetFeature.restype = wintypes.BOOL

kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
kernel32.ReleaseMutex.argtypes = [ctypes.wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]


def build_query() -> bytes:
    payload = bytearray(64)
    payload[0] = CMD_BATTERY
    payload[7] = sum(payload[:7]) & 0x7F
    frame = bytearray(FEATURE_LEN)
    frame[1:] = payload
    return bytes(frame)


def _parse(packet: bytes | None):
    """Return ``(level, charging, full)`` or None."""
    if not packet or len(packet) < 4:
        return None
    payload = packet[1:] if packet[0] == 0 else packet
    if len(payload) < 8:
        return None
    level = payload[1]
    status = payload[2]
    if level > 100:
        return None
    if level == 0 and status == 0 and not any(payload[1:8]):
        return None
    # Awake keyboards keep this tail. A zeroed or unrelated report does not.
    if payload[3:7] != b"\x00\x01\x01\x01":
        return None
    return level, status == 1, status not in (0, 1)


def _exchange(handle, frame: bytes):
    buf = ctypes.create_string_buffer(frame, FEATURE_LEN)
    hid.HidD_SetFeature(handle, buf, FEATURE_LEN)
    time.sleep(0.04)
    got = ctypes.create_string_buffer(FEATURE_LEN)
    if not hid.HidD_GetFeature(handle, got, FEATURE_LEN):
        return None
    return bytes(got.raw)


def find_receivers() -> list[HidDevice]:
    devices = []
    for dev in enumerate_hid():
        if dev.vid != VID or dev.pid not in PIDS:
            continue
        if dev.usage_page == USAGE_PAGE and dev.usage == USAGE and dev.feature_len == FEATURE_LEN:
            devices.append(dev)
    return devices


def _locked(fn):
    mutex = kernel32.CreateMutexW(None, False, HID_LOCK)
    if not mutex:
        return fn()
    waited = kernel32.WaitForSingleObject(mutex, 500)
    if waited not in (0, 0x80):
        kernel32.CloseHandle(mutex)
        return Reading(False, NAME, None, None, None, "另一个程序正在读电量", "")
    try:
        return fn()
    finally:
        kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)


def read_battery(debug=None) -> Reading:
    def log(text: str) -> None:
        if debug:
            debug(text)

    receivers = find_receivers()
    if not receivers:
        return Reading(False, NAME, None, None, None, "没找到接收器", "VID 3151")

    def once() -> Reading:
        last = Reading(False, NAME, None, None, None, "没有回复，动一下键盘", "")
        frame = build_query()
        for dev in receivers:
            link = "有线" if dev.pid == 0x4010 else "2.4G"
            source = f"{link} · {dev.vid:04X}:{dev.pid:04X} · {dev.product or NAME}"
            handle = _open(dev.path, GENERIC_READ | GENERIC_WRITE) or _open(dev.path, GENERIC_READ)
            if handle is None:
                last = Reading(False, NAME, None, None, None, "打不开配置口", source, dev.vid, dev.pid)
                log("open failed")
                continue
            try:
                log("TX battery " + frame[:10].hex())
                raw = _exchange(handle, frame)
                log("RX battery " + (raw[:12].hex() if raw else "none"))
                parsed = _parse(raw)
                if parsed is None:
                    last = Reading(False, NAME, None, None, None, "没有回复，动一下键盘", source, dev.vid, dev.pid)
                    continue
                level, charging, full = parsed
                if charging:
                    detail = "充电中"
                elif full:
                    detail = "已充满"
                else:
                    detail = "使用电池"
                log(f"RX battery {level} charge {int(charging)} full {int(full)}")
                return Reading(True, NAME, level, charging, None, detail, source, dev.vid, dev.pid)
            finally:
                kernel32.CloseHandle(handle)
        return last

    return _locked(once)


def read_batteries(debug=None) -> list[Reading]:
    if not find_receivers():
        return []
    return [read_battery(debug)]
