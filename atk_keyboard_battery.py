"""ATK V75X battery query.

The official hub (v3-hub.atkgear.com) talks to this keyboard with the Evision
packet class. WebHID ``sendReport(4, data)`` is report id ``0x04`` plus a
63-byte buffer. Windows therefore writes 64 bytes.

Buffer, without the report id:

* ``[0:2]`` request code, little-endian. The reply echoes it.
* ``[2]`` command. ``26`` is power, ``255`` is the dongle's error.
* ``[7]`` percent, capped at 100
* ``[8]`` ``2`` while charging

Verified on the receiver plugged into this machine:

* product ``ATK V75X 2.4G Dongle``, VID ``0x320F``, PID ``0x5088``
  (wired in the same table is ``0x5055``)
* collection usage page ``0xFF1C``, usage ``0x0092``, input 64, output 64

A sleeping keyboard answers command byte ``0xFF`` (or nothing). That is not 0%.
The hub names this dongle as keymap ``kb_75``.
"""

from __future__ import annotations

import ctypes
import time

from delux_battery import Reading
from hid_win import HidDevice, HidIo, enumerate_hid, kernel32

VID = 0x320F
PIDS = {0x5088, 0x5055}
USAGE_PAGE = 0xFF1C
USAGE = 0x0092
REPORT_ID = 0x04
WIRE_LEN = 64
CMD_POWER = 26
DATA_PERCENT = 7
DATA_CHARGE = 8

HID_LOCK = "Local\\AtkV75xBatteryHid"
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
kernel32.ReleaseMutex.argtypes = [ctypes.wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]


def build_frame(code: int, command: int) -> bytes:
    frame = bytearray(WIRE_LEN)
    frame[0] = REPORT_ID
    frame[1] = code & 0xFF
    frame[2] = (code >> 8) & 0xFF
    frame[3] = command & 0xFF
    return bytes(frame)


def _body(packet: bytes | None) -> bytes | None:
    if not packet:
        return None
    if packet[0] == REPORT_ID and len(packet) >= 10:
        return packet[1:]
    if len(packet) >= 9:
        return packet
    return None


def _parse(packet: bytes | None, code: int):
    """Return ``(level, charging)``, ``"error"``, ``"empty"``, or None if unrelated."""
    body = _body(packet)
    if body is None:
        return None
    got = body[0] | (body[1] << 8)
    if got != code:
        return None
    if body[2] == 0xFF:
        return "error"
    if body[2] != CMD_POWER or len(body) <= DATA_CHARGE:
        return None
    level = body[DATA_PERCENT]
    if level > 100:
        return "bad"
    charging = body[DATA_CHARGE] == 2
    if level == 0 and not charging and not any(body[DATA_PERCENT:DATA_PERCENT + 8]):
        return "empty"
    return level, charging


def _transact(io: HidIo, frame: bytes, code: int, timeout_s: float):
    deadline = time.monotonic() + timeout_s
    next_send = 0.0
    saw_error = False
    saw_empty = False
    last = None
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_send:
            io.write(frame)
            next_send = now + 0.35
        packet = io.read(40)
        if packet:
            last = bytes(packet[:16])
        parsed = _parse(packet, code)
        if parsed is None:
            continue
        if parsed == "error":
            return None, True, saw_empty, last
        if parsed == "empty":
            return None, saw_error, True, last
        if parsed == "bad":
            continue
        return parsed, saw_error, saw_empty, last
    return None, saw_error, saw_empty, last


def find_receivers() -> list[HidDevice]:
    devices = []
    for dev in enumerate_hid():
        if dev.vid != VID or dev.pid not in PIDS:
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
        return Reading(False, "ATK V75X", None, None, None, "另一个程序正在读电量", "")
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
        return Reading(False, "ATK V75X", None, None, None, "没找到接收器", "VID 320F")

    def once() -> Reading:
        last = Reading(False, "ATK V75X", None, None, None, "没有回复，动一下键盘", "")
        for dev in receivers:
            source = f"2.4G · {dev.vid:04X}:{dev.pid:04X} · {dev.product or 'ATK V75X'}"
            try:
                with HidIo(dev.path, dev.input_len or WIRE_LEN) as io:
                    stale = io.read(30)
                    if stale:
                        log("stale " + bytes(stale[:16]).hex())
                    frame = build_frame(1, CMD_POWER)
                    log("TX power " + frame[:8].hex())
                    parsed, saw_error, saw_empty, raw = _transact(io, frame, 1, 0.6)
                    log("RX power " + (raw.hex() if raw else "none"))
                    if parsed is None:
                        if saw_error or saw_empty:
                            detail = "键盘休眠或没连上，动一下键盘"
                        else:
                            detail = "没有回复，动一下键盘"
                        last = Reading(False, "ATK V75X", None, None, None, detail, source, dev.vid, dev.pid)
                        continue
                    level, charging = parsed
                    detail = "充电中" if charging else "使用电池"
                    log(f"RX power {level} charge {int(charging)}")
                    return Reading(True, "ATK V75X", level, charging, None, detail, source, dev.vid, dev.pid)
            except OSError as exc:
                last = Reading(False, "ATK V75X", None, None, None, f"读取失败：{exc}", source, dev.vid, dev.pid)
                log(str(exc))
        return last

    return _locked(once)


def read_batteries(debug=None) -> list[Reading]:
    if not find_receivers():
        return []
    return [read_battery(debug)]
