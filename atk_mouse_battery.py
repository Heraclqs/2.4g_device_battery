"""ATK F1 battery query.

The official hub (v3-hub.atkgear.com) uses the Compx 17-byte frame, the same
layout as the Delux reader. Report id is ``0x08``. Checksum is
``(0x55 - sum(first 16 bytes)) & 0xFF``.

Verified on the receiver plugged into this machine:

* product ``ATK Mouse 1K Dongle``, VID ``0x373B``, PID ``0x101A``
  (8K dongle in the same catalog is ``0x101B``)
* collection usage page ``0xFF02``, usage ``0x0002``, 17-byte input and output
* identity command returned cid 2, mid 56, type 0
* hub catalog ``2,56`` is ATK F1 LEVIATAN, sensor PAW3950Ultra

Battery reply, indexes on the 17-byte Windows buffer (report id included):

* ``[6]`` percent, ``[7]`` 1 while charging, ``[8:10]`` voltage in mV, big-endian

Link command ``0x03`` puts ``1`` at ``[6]`` only while the mouse is awake.
A sleeping mouse still answers identity from the dongle cache, and does not
answer the battery command. That is not 0%.
"""

from __future__ import annotations

import ctypes
import time

from delux_battery import Reading, build_frame, checksum, dpi_value_for, peek_dpi_for, query_dpi
from hid_win import HidDevice, HidIo, enumerate_hid, kernel32

CMD_INFO = 0x01
CMD_ONLINE = 0x03
CMD_BATTERY = 0x04
REPORT_LEN = 17

VID = 0x373B
# 4122 is the 1 kHz dongle, 4123 is the 8 kHz dongle.
PIDS = {0x101A, 0x101B}
USAGE_PAGE = 0xFF02
USAGE = 0x0002

# cid, mid from the hub catalog. Only F1-family mice are listed.
PROFILES = {
    (2, 95): ("DRAGONFLY F1 V2 SE NK", "PAW3311"),
    (2, 1): ("ATK Dragonfly F1 PRO", "PAW3395"),
    (2, 2): ("ATK Dragonfly F1 PRO", "PAW3395"),
    (2, 104): ("ATK Dragonfly F1 PRO", "PAW3395"),
    (2, 3): ("ATK Dragonfly F1 PRO MAX", "PAW3395"),
    (2, 4): ("ATK Dragonfly F1 PRO MAX", "PAW3395"),
    (2, 105): ("ATK Dragonfly F1 PRO MAX", "PAW3395"),
    (2, 5): ("ATK Dragonfly F1 MOBA", "PAW3395"),
    (2, 6): ("ATK Dragonfly F1 MOBA", "PAW3395"),
    (2, 33): ("ATK Dragonfly F1 MOBA", "PAW3395"),
    (2, 106): ("ATK Dragonfly F1 MOBA", "PAW3395"),
    (2, 48): ("ATK Dragonfly F1 PRO MAX Elden", "PAW3395"),
    (2, 52): ("ATK Dragonfly F1 PRO MAX JOJO", "PAW3395"),
    (2, 19): ("ATK Dragonfly F1S", "PAW3395"),
    (2, 20): ("ATK Dragonfly F1S", "PAW3395"),
    (2, 93): ("DRAGONFLY F1 V2 Plus NK", "PAW3395"),
    (2, 94): ("DRAGONFLY F1 V2 NK", "PAW3395SE"),
    (2, 110): ("DRAGONFLY F1 V2 PRO MAX", "PAW3395Ultra"),
    (1, 12): ("ATK F1 Ultra Max 2.0", "PAW3395Ultra"),
    (2, 36): ("ATK F1 PRO", "PAW3950"),
    (2, 37): ("ATK F1 PRO MAX", "PAW3950"),
    (2, 42): ("ATK F1 Ultimate", "PAW3950Ultra"),
    (2, 45): ("ATK F1 Ultra", "PAW3950Ultra"),
    (2, 50): ("ATK F1 EXTREME", "PAW3950Ultra"),
    (2, 56): ("ATK F1 LEVIATAN", "PAW3950Ultra"),
    (1, 8): ("ATK F1 Ultimate 2.0", "PAW3950Ultra"),
    (1, 7): ("ATK F1 EXTREME 2.0", "PAW3950Ultra"),
    (1, 13): ("ATK F1 LEVIATAN 2.0", "PAW3950Ultra"),
    (1, 33): ("ATK F1 Ultra Max 2.0", "PAW3950Ultra"),
    (1, 85): ("ATK F1 V3 ULTIMATE", "PAW3950Ultra"),
    (1, 58): ("ATK F1 V3 EXTREME +", ""),
    (1, 60): ("ATK F1 V3 LEVIATAN +", "PAW3955MASTER"),
    (1, 59): ("ATK F1 Ultimate 2.0 +", "PAW3955MASTER"),
    (1, 62): ("ATK F1 V3 ULTIMATE +", "PAW3955MASTER"),
    (1, 63): ("ATK F1 V3 ULTIMATE +", "PAW3955MASTER"),
    (1, 64): ("ATK F1 V3 ULTIMATE +", "PAW3955MASTER"),
    (1, 90): ("ATK F1 V3 PIKACHU Edition", "PAW3955MASTER"),
}

HID_LOCK = "Local\\AtkF1BatteryHid"
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
kernel32.ReleaseMutex.argtypes = [ctypes.wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]


def _valid(packet: bytes | None, command: int) -> bool:
    if packet is None or len(packet) < REPORT_LEN:
        return False
    if packet[0] != 0x08 or packet[1] != command or packet[2] != 0:
        return False
    return packet[16] == checksum(packet)


def _transact(io: HidIo, frame: bytes, timeout_s: float, accept) -> tuple[bytes | None, bool]:
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
        if dev.vid != VID or dev.pid not in PIDS:
            continue
        if dev.usage_page == USAGE_PAGE and dev.usage == USAGE and dev.output_len == REPORT_LEN:
            devices.append(dev)
    return devices


def _profile_name(cid: int | None, mid: int | None) -> tuple[str, str]:
    if cid is None or mid is None:
        return "ATK F1", ""
    profile = PROFILES.get((cid, mid))
    if profile is None:
        return f"ATK 鼠标 cid {cid} mid {mid}", ""
    return profile


def _locked(fn):
    mutex = kernel32.CreateMutexW(None, False, HID_LOCK)
    if not mutex:
        return fn()
    waited = kernel32.WaitForSingleObject(mutex, 500)
    if waited not in (0, 0x80):
        kernel32.CloseHandle(mutex)
        return Reading(False, "ATK F1", None, None, None, "另一个程序正在读电量", "")
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
        return Reading(False, "ATK F1", None, None, None, "没找到接收器", "VID 373B")

    def once() -> Reading:
        last = Reading(False, "ATK F1", None, None, None, "没有回复，动一下鼠标", "")
        for dev in receivers:
            source = f"2.4G · {dev.vid:04X}:{dev.pid:04X} · {dev.product or 'ATK F1'}"
            try:
                with HidIo(dev.path, dev.input_len or REPORT_LEN) as io:
                    stale = io.read(40)
                    if stale:
                        log("stale " + stale.hex())
                    info_frame = build_frame(CMD_INFO, bytes([0x11, 0x22, 0x33, 0x44, 0, 0, 0, 0]))
                    online_frame = build_frame(CMD_ONLINE)
                    battery_frame = build_frame(CMD_BATTERY)
                    log("TX online " + online_frame.hex())
                    online, saw_online = _transact(io, online_frame, 0.45, lambda packet: True)
                    log("RX online " + (online.hex() if online else "none"))
                    if online is None or online[6] != 1:
                        detail = "没有回复，动一下鼠标" if not saw_online else "鼠标休眠或没连上，动一下鼠标"
                        last = Reading(False, "ATK F1", None, None, None, detail, source, dev.vid, dev.pid)
                        continue
                    log("TX info " + info_frame.hex())
                    info, _saw_info = _transact(io, info_frame, 0.35, lambda packet: True)
                    log("RX info " + (info.hex() if info else "none"))
                    cid = info[10] if info else None
                    mid = info[11] if info else None
                    name, sensor = _profile_name(cid, mid)
                    log("TX battery " + battery_frame.hex())
                    battery, _saw_battery = _transact(io, battery_frame, 0.6, lambda packet: packet[6] <= 100)
                    log("RX battery " + (battery.hex() if battery else "none"))
                    if battery is None:
                        last = Reading(False, name, None, None, None, "在线但没有电量回包", source, dev.vid, dev.pid, cid, mid)
                        continue
                    level = battery[6]
                    if level > 100:
                        last = Reading(False, name, None, None, None, f"电量字节异常：{level}", source, dev.vid, dev.pid, cid, mid)
                        continue
                    charging = battery[7] == 1
                    voltage = (battery[8] << 8) | battery[9]
                    detail = "充电中" if charging else "使用电池"
                    if sensor:
                        detail = f"{detail} · {sensor}"
                    if voltage:
                        detail = f"{detail} · {voltage / 1000:.3f} V"
                    return Reading(
                        True, name, level, charging, voltage or None, detail, source,
                        dev.vid, dev.pid, cid, mid,
                    )
            except OSError as exc:
                last = Reading(False, "ATK F1", None, None, None, f"读取失败：{exc}", source, dev.vid, dev.pid)
                log(str(exc))
        return last

    return _locked(once)


def peek_dpi() -> Reading | None:
    return peek_dpi_for(find_receivers(), "ATK F1", HID_LOCK, timeout=0.04)


def read_batteries(debug=None) -> list[Reading]:
    if not find_receivers():
        return []
    return [read_battery(debug)]
