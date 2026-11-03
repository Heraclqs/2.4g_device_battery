"""VGN811 battery query.

The official VGN Driver (rongyuan SDK v130) reads this keyboard with feature
command ``0x83`` (``FEA_CMD_GET_BATTERY``). Checksum type is BIT7: byte 7 of
the 64-byte payload is the low 7 bits of the sum of bytes 0..6.

Windows feature reports are 65 bytes. Byte 0 is report id ``0``. The SDK's
reply buffer is the following 64 bytes:

* ``[0]`` changes between ``0x00`` and ``0x01`` while the keyboard is awake. It is not the command echo.
* ``[1]`` percent, capped at 100
* ``[2]`` ``0`` on battery, ``1`` while charging, any other value means full.
  The official driver uses only this byte.
* ``[3]`` also becomes ``1`` after the backlight times out, while ``[2]`` stays ``0``.
  A live idle report was ``00 00 5A 00 01 01 00 01 79`` (90%, not charging).
  Cable charging is the wired interface ``4010`` being present, not this byte.

Verified on the receiver plugged into this machine:

* product ``2.4G Wireless Keyboard``, manufacturer ``ROYUAN``
* VID ``0x3151``, PID ``0x4011`` (wired in the same table is ``0x4010``)
* collection usage page ``0xFFFF``, usage ``0x0002``, feature report 65
* one feature read came back ``00 00 44 00 00 01 01 01 79 ...`` (68%)
* a later read came back ``00 01 45 00 00 01 01 01 79 ...`` (69%)

After the PC boots with the keyboard in 2.4G, command ``0x83`` does not
come back: the feature buffer stays empty or holds the dongle id
``AA 55``. The dongle answers ``0xF7`` locally. On this machine that
snapshot was ``00 00 61 00 00 01 01 01 00`` (97%, on battery). Byte 7 is ``0`` there,
not ``0x79``.

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


CMD_DONGLE = 0xF7


def build_frame(cmd: int, checksum: str = "low7") -> bytes:
    """Feature report. Byte 0 is report id ``0``.

    ``low7`` is the keyboard command checksum (``sum & 0x7F``).
    ``inv`` is the dongle-local checksum (``255 - (sum & 255)``), used by ``0xF7``.
    """
    payload = bytearray(64)
    payload[0] = cmd
    total = sum(payload[:7])
    if checksum == "inv":
        payload[7] = (255 - (total & 255)) & 255
    else:
        payload[7] = total & 0x7F
    frame = bytearray(FEATURE_LEN)
    frame[1:] = payload
    return bytes(frame)


def build_query() -> bytes:
    return build_frame(CMD_BATTERY, "low7")


def _parse(packet: bytes | None, *, allow_dongle: bool = False):
    """Return ``(level, charging, full)`` or None."""
    if not packet or len(packet) < 4:
        return None
    payload = packet[1:] if packet[0] == 0 else packet
    if len(payload) < 8:
        return None
    # Sticky dongle-id report left behind by 0xFD. Not a battery.
    if payload[0] == 0xAA and payload[1] == 0x55:
        return None
    level = payload[1]
    status = payload[2]
    if level > 100:
        return None
    if level == 0 and status == 0 and not any(payload[1:8]):
        return None
    # Awake marker. A keyboard echo also ends in 0x79. After a cold boot the
    # dongle status snapshot keeps the marker and leaves byte 7 at 0.
    if payload[4] != 0x01 or payload[6] != 0x01:
        return None
    if not allow_dongle and payload[7] != 0x79:
        return None
    # [3] == 1 also happens when the backlight has timed out and the cable is
    # unplugged. The driver treats only [2] as the charge state.
    charging = status == 1
    full = status not in (0, 1)
    return level, charging, full


def _exchange(handle, frame: bytes):
    buf = ctypes.create_string_buffer(frame, FEATURE_LEN)
    hid.HidD_SetFeature(handle, buf, FEATURE_LEN)
    time.sleep(0.04)
    got = ctypes.create_string_buffer(FEATURE_LEN)
    if not hid.HidD_GetFeature(handle, got, FEATURE_LEN):
        return None
    return bytes(got.raw)


def cable_attached() -> bool:
    """The keyboard enumerates ``3151:4010`` only while the USB cable is in."""
    return any(dev.vid == VID and dev.pid == 0x4010 for dev in enumerate_hid())


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
        # The cable interface answers with a different feature report. The
        # dongle is the one that has the battery, including while charging.
        radio = [dev for dev in receivers if dev.pid == 0x4011]
        ordered = radio or receivers
        for dev in ordered:
            link = "有线" if dev.pid == 0x4010 else "2.4G"
            source = f"{link} · {dev.vid:04X}:{dev.pid:04X} · {dev.product or NAME}"
            handle = _open(dev.path, GENERIC_READ | GENERIC_WRITE) or _open(dev.path, GENERIC_READ)
            if handle is None:
                last = Reading(False, NAME, None, None, None, "打不开配置口", source, dev.vid, dev.pid)
                log("open failed")
                continue
            try:
                queries = [(build_query(), False)]
                if dev.pid == 0x4011:
                    queries.insert(0, (build_frame(CMD_DONGLE, "inv"), True))
                for frame, allow_dongle in queries:
                    log("TX battery " + frame[:10].hex())
                    raw = _exchange(handle, frame)
                    parsed = _parse(raw, allow_dongle=allow_dongle)
                    if parsed is None and allow_dongle:
                        raw = _exchange(handle, frame)
                        parsed = _parse(raw, allow_dongle=True)
                    log("RX battery " + (raw[:12].hex() if raw else "none"))
                    if parsed is None:
                        continue
                    level, charging, full = parsed
                    if not charging and not full and cable_attached():
                        charging = True
                    if charging:
                        detail = "充电中"
                    elif full:
                        detail = "已充满"
                    else:
                        detail = "使用电池"
                    log(f"RX battery {level} charge {int(charging)} full {int(full)}")
                    return Reading(True, NAME, level, charging, None, detail, source, dev.vid, dev.pid)
                last = Reading(False, NAME, None, None, None, "没有回复，动一下键盘", source, dev.vid, dev.pid)
            finally:
                kernel32.CloseHandle(handle)
        return last

    return _locked(once)


def read_batteries(debug=None) -> list[Reading]:
    if not find_receivers():
        return []
    return [read_battery(debug)]
