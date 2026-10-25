"""INPHIC mouse battery reports.

The web hub (hub.inphic.cn) does not open the dongle. It listens on
``ws://localhost:10085``, which INPHIC HUB 1.0.1.24 serves. A live frame from
this machine was ``82010398400164``:

* ``130`` high-speed 2.4G, ``152`` IN10R, ``0x40`` battery, status ``1``, ``100%``

The same client reads the dongle when the hub is not already running:

* product ``Inphic KP 8K``, VID ``0x1D57``, PID ``0xFA65`` (the other id is ``FA60``)
* collection usage page ``0xFF00``, usage ``0x0001``, report id ``0x04``, 64 bytes
* ``[1]`` model, ``[2] & 0x40`` battery, ``[3]`` status, ``[4]`` percent

The dongle pushes that report. A sleeping mouse sends nothing until it is moved.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import socket
import time
from pathlib import Path

from delux_battery import Reading
from hid_win import HidDevice, HidIo, enumerate_hid, kernel32

REPORT_ID = 0x04
REPORT_LEN = 64
USAGE_PAGE = 0xFF00
USAGE = 0x0001
PIDS = {0xFA60, 0xFA65}
VID = 0x1D57

# model id -> name, copied from the web hub's mouseNum table
MODELS = {
    1: "X3 Pro",
    6: "X8 Plus",
    7: "X11 SE",
    16: "R1",
    48: "M900 Pro",
    61: "M900 Mini",
    65: "IN6",
    66: "In9",
    67: "G15",
    77: "X3",
    78: "X3 Max",
    85: "X11",
    113: "IN10 Pro",
    114: "IN103D",
    115: "IN10R Max",
    116: "IN10 Max",
    133: "X6",
    144: "IN9 Ultra",
    146: "IN6 SE",
    147: "IN10",
    149: "IN9 Pro",
    152: "IN10R",
    153: "INX",
    177: "X1",
    188: "M3",
    190: "X11 Pro",
    195: "G9",
    201: "G9 PRO",
}

HID_LOCK = "Local\\Inphic24GBatteryHid"
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
kernel32.ReleaseMutex.argtypes = [ctypes.wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]


def find_receivers() -> list[HidDevice]:
    devices = []
    for dev in enumerate_hid():
        if dev.vid != VID or dev.pid not in PIDS:
            continue
        if dev.usage_page == USAGE_PAGE and dev.usage == USAGE and dev.input_len == REPORT_LEN:
            devices.append(dev)
    return devices


LINKS = {
    132: "蓝牙",
    130: "2.4G 8K",
    131: "2.4G 8K",
    129: "2.4G",
    128: "有线",
    133: "有线",
}
HUB_PORT = 10085


def _model_name(device_id: int) -> str:
    model = MODELS.get(device_id)
    if model:
        return f"英菲克 {model}"
    return f"英菲克鼠标 {device_id}"


def _from_status(status: int, level: int) -> tuple[bool, int | None, bool | None, str]:
    if status == 3:
        shown = level if 0 <= level <= 100 else None
        return True, shown, True, "充电中"
    if status == 2:
        return True, 100, False, "已充满"
    if status == 1 and 0 <= level <= 100:
        return True, level, False, "使用电池"
    return False, None, None, f"电量状态异常：{status}"


# Factory X counts. The live frame only carries the gear id. A customized
# table saved by hub.inphic.cn replaces these when it is present.
DPI_VALUES = {
    152: {1: 800, 2: 1600, 3: 3200, 4: 6500, 5: 13000, 6: 26000},
}
_STORAGE_KEY = {
    152: "IN10R",
    115: "IN10RMax",
    113: "IN10Pro",
    114: "IN103D",
    116: "IN10Max",
    147: "IN10",
    149: "IN9Pro",
    144: "IN9Ultra",
    66: "In9",
    61: "M900Mini",
    48: "M900Pro",
}
_saved_cache: tuple[float, dict[str, dict[int, int]]] | None = None
_DPI_VALUE = re.compile(r'"([A-Za-z0-9]+)":\{"dpiID":\d+,"dpi_ON_OFF_flag":\d+,"dpiValue":\{([^}]+)\}')
_DPI_SLOT = re.compile(r'"dpi(\d+)":(\d+)')


def _uvarint(data: bytes, index: int) -> tuple[int, int]:
    number = shift = 0
    while index < len(data):
        byte = data[index]
        index += 1
        number |= (byte & 0x7F) << shift
        if byte < 128:
            return number, index
        shift += 7
    raise ValueError("varint")


def _snappy_copy(out: bytearray, offset: int, length: int) -> None:
    if offset <= 0 or offset > len(out):
        raise ValueError("offset")
    for _ in range(length):
        out.append(out[-offset])


def _snappy(data: bytes) -> bytes:
    length, index = _uvarint(data, 0)
    if not 400 <= length <= 80000:
        raise ValueError("length")
    out = bytearray()
    while index < len(data) and len(out) < length:
        tag = data[index]
        index += 1
        kind = tag & 3
        if kind == 0:
            size = tag >> 2
            if size < 60:
                size += 1
            elif size == 60:
                size = data[index] + 1
                index += 1
            elif size == 61:
                size = int.from_bytes(data[index:index + 2], "little") + 1
                index += 2
            elif size == 62:
                size = int.from_bytes(data[index:index + 3], "little") + 1
                index += 3
            else:
                size = int.from_bytes(data[index:index + 4], "little") + 1
                index += 4
            out += data[index:index + size]
            index += size
        elif kind == 1:
            _snappy_copy(out, ((tag >> 5) << 8) | data[index], ((tag >> 2) & 7) + 4)
            index += 1
        elif kind == 2:
            _snappy_copy(out, int.from_bytes(data[index:index + 2], "little"), (tag >> 2) + 1)
            index += 2
        else:
            _snappy_copy(out, int.from_bytes(data[index:index + 4], "little"), (tag >> 2) + 1)
            index += 4
    if len(out) < length:
        raise ValueError("short")
    return bytes(out[:length])


def _storage_files() -> list[Path]:
    local = os.environ.get("LOCALAPPDATA", "")
    folders = [
        Path(local) / "Microsoft" / "Edge" / "User Data" / "Default" / "Local Storage" / "leveldb",
        Path(local) / "Google" / "Chrome" / "User Data" / "Default" / "Local Storage" / "leveldb",
    ]
    found = []
    for folder in folders:
        if folder.is_dir():
            found.extend(folder.glob("*.ldb"))
    found.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return found


def _tables_in(blob: bytes) -> dict[str, dict[int, int]]:
    tables: dict[str, dict[int, int]] = {}
    cursor = 0
    while True:
        at = blob.find(b"dpiData", cursor)
        if at < 0:
            break
        cursor = at + 7
        for offset in range(max(0, at - 8000), at):
            try:
                text = _snappy(blob[offset:offset + 150000]).decode("utf-8", "replace")
            except (ValueError, IndexError, UnicodeError):
                continue
            found = {}
            for name, body in _DPI_VALUE.findall(text):
                slots = {int(gear): int(value) for gear, value in _DPI_SLOT.findall(body)}
                if slots:
                    found[name] = slots
            if found:
                tables = found
                break
    return tables


def _saved_tables() -> dict[str, dict[int, int]]:
    """Latest DPI counts saved by the official hub page. Empty when absent."""
    global _saved_cache
    files = _storage_files()
    stamp = files[0].stat().st_mtime if files else 0.0
    if _saved_cache is not None and _saved_cache[0] == stamp:
        return _saved_cache[1]
    tables: dict[str, dict[int, int]] = {}
    for path in files:
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        if b"dpiData" not in blob:
            continue
        tables = _tables_in(blob)
        if tables:
            break
    _saved_cache = (stamp, tables)
    return tables


def dpi_number(device_id: int, gear: int) -> int | None:
    key = _STORAGE_KEY.get(device_id)
    if key:
        saved = _saved_tables().get(key)
        if saved and gear in saved:
            value = saved[gear]
            if value:
                return value
    table = DPI_VALUES.get(device_id)
    if table is None:
        return None
    return table.get(gear)


def parse_dpi_frame(nums: list[int]) -> tuple[int, int] | None:
    """Hub push. Type byte 16, then the 1-based gear id."""
    if len(nums) == 7 and nums[4] == 16:
        device_id, gear = nums[3], nums[5]
    elif len(nums) == 6 and nums[3] == 16:
        device_id, gear = nums[2], nums[4]
    else:
        return None
    if not 1 <= gear <= 8:
        return None
    return device_id, gear


def parse_hid_dpi(packet: bytes) -> tuple[int, int] | None:
    """Same fields as the hub frame, with the report id in front."""
    if len(packet) < 4 or packet[0] != REPORT_ID:
        return None
    if packet[2] not in (0x10, 0x11):
        return None
    gear = packet[3]
    if not 1 <= gear <= 8:
        return None
    return packet[1], gear


def _parse(packet: bytes) -> tuple[int, int, int] | None:
    if len(packet) < 5 or packet[0] != REPORT_ID:
        return None
    if (packet[2] & 0x40) == 0:
        return None
    return packet[1], packet[3], packet[4]


def _locked(fn):
    mutex = kernel32.CreateMutexW(None, False, HID_LOCK)
    if not mutex:
        return fn()
    waited = kernel32.WaitForSingleObject(mutex, 500)
    if waited not in (0, 0x80):
        kernel32.CloseHandle(mutex)
        return [Reading(False, "英菲克鼠标", None, None, None, "另一个程序正在读电量", "")]
    try:
        return fn()
    finally:
        kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)


def _read_one(dev: HidDevice, debug) -> list[Reading]:
    link = "2.4G 8K" if dev.pid == 0xFA65 else "2.4G"
    source = f"{link} · {dev.vid:04X}:{dev.pid:04X} · {dev.product or 'INPHIC'}"

    def log(text: str) -> None:
        if debug:
            debug(text)

    found: dict[int, tuple[int, int]] = {}
    try:
        with HidIo(dev.path, dev.input_len or REPORT_LEN) as io:
            deadline = time.monotonic() + 2.4
            while time.monotonic() < deadline:
                packet = io.read(200)
                if not packet:
                    continue
                log("RX " + packet[:12].hex())
                parsed = _parse(packet)
                if parsed is None:
                    continue
                device_id, status, level = parsed
                found[device_id] = (status, level)
                break
    except OSError as exc:
        log(str(exc))
        return [Reading(False, "英菲克鼠标", None, None, None, f"读取失败：{exc}", source, dev.vid, dev.pid)]

    if not found:
        return [Reading(
            False, "英菲克鼠标", None, None, None,
            "没有电量回报。先关掉官方 INPHIC HUB，再动一下鼠标",
            source, dev.vid, dev.pid,
        )]

    readings = []
    for device_id, (status, level) in found.items():
        ok, shown, charging, detail = _from_status(status, level)
        readings.append(Reading(
            ok, _model_name(device_id), shown, charging, None, detail, source,
            dev.vid, dev.pid,
        ))
    return readings


def _hub_message(nums: list[int]) -> tuple[int, int, int, int] | None:
    """Web-hub battery frame. Returns model, status, level, connection."""
    if len(nums) not in (6, 7) or not 128 <= nums[0] <= 133:
        return None
    if len(nums) == 7:
        if nums[4] not in (0x40, 0x41):
            return None
        return nums[3], nums[5], nums[6], nums[0]
    if nums[3] not in (0x40, 0x41):
        return None
    return nums[2], nums[4], nums[5], nums[0]


def _ws_frames(blob: bytearray) -> tuple[list[bytes], bytearray]:
    frames: list[bytes] = []
    while len(blob) >= 2:
        length = blob[1] & 0x7F
        masked = blob[1] & 0x80
        pos = 2
        if length == 126:
            if len(blob) < 4:
                break
            length = int.from_bytes(blob[2:4], "big")
            pos = 4
        elif length == 127:
            break
        if masked:
            pos += 4
        if len(blob) < pos + length:
            break
        if (blob[0] & 0x0F) == 0x1:
            frames.append(bytes(blob[pos:pos + length]))
        del blob[: pos + length]
    return frames, blob


_HUB_FILE = Path(os.environ.get("APPDATA", ".")) / "2.4gDeviceBattery" / "inphic_battery.json"
_dongle_pid = 0
_dongle_at = 0.0


def _receiver_pid() -> int:
    global _dongle_pid, _dongle_at
    now = time.monotonic()
    if now - _dongle_at < 5:
        return _dongle_pid
    dongle = find_receivers()
    _dongle_pid = dongle[0].pid if dongle else 0
    _dongle_at = now
    return _dongle_pid


def _reading_from_hub(device_id: int, status: int, level: int, link: int) -> Reading:
    ok, shown, charging, detail = _from_status(status, level)
    source = f"{LINKS.get(link, '2.4G')} · INPHIC HUB"
    return Reading(
        ok, _model_name(device_id), shown, charging, None, detail, source, VID, _receiver_pid(),
    )


def remember_hub(nums: list[int]) -> None:
    """Keep the latest hub battery frame so a refresh does not wait for the next push."""
    parsed = _hub_message(nums)
    if parsed is None:
        return
    reading = _reading_from_hub(*parsed)
    try:
        _HUB_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HUB_FILE.write_text(json.dumps({
            "t": time.time(),
            "ok": reading.ok,
            "name": reading.name,
            "level": reading.level,
            "charging": reading.charging,
            "detail": reading.detail,
            "source": reading.source,
            "vid": reading.vid,
            "pid": reading.pid,
        }), encoding="utf-8")
    except OSError:
        pass


def _cached_hub(max_age: float = 12.0) -> list[Reading] | None:
    try:
        payload = json.loads(_HUB_FILE.read_text(encoding="utf-8"))
        if time.time() - float(payload["t"]) > max_age:
            return None
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return [Reading(
        bool(payload.get("ok")), str(payload.get("name") or "英菲克鼠标"),
        payload.get("level"), payload.get("charging"), None,
        str(payload.get("detail") or ""), str(payload.get("source") or "INPHIC HUB"),
        int(payload.get("vid") or VID), int(payload.get("pid") or 0),
    )]


def _read_hub(debug, timeout: float = 0.6) -> list[Reading] | None:
    """Same socket the web driver uses while INPHIC HUB is running."""

    def log(text: str) -> None:
        if debug:
            debug(text)

    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        "GET / HTTP/1.1\r\n"
        "Host: localhost:10085\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    try:
        sock = socket.create_connection(("127.0.0.1", HUB_PORT), 0.6)
    except OSError:
        return None
    sock.settimeout(0.4)
    try:
        sock.sendall(request.encode())
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in buf and time.monotonic() < deadline:
            buf += sock.recv(1024)
        header, _, rest = bytes(buf).partition(b"\r\n\r\n")
        if b"101" not in header.split(b"\r\n", 1)[0]:
            return None
        pending = bytearray(rest)
        while time.monotonic() < deadline:
            frames, pending = _ws_frames(pending)
            for frame in frames:
                text = frame.decode("ascii", errors="replace").strip()
                if len(text) < 2 or len(text) % 2:
                    continue
                try:
                    nums = [int(text[i:i + 2], 16) for i in range(0, len(text), 2)]
                except ValueError:
                    continue
                log("hub " + text)
                parsed = _hub_message(nums)
                if parsed is None:
                    continue
                device_id, status, level, link = parsed
                remember_hub(nums)
                return [_reading_from_hub(device_id, status, level, link)]
            try:
                pending += sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
    except OSError as exc:
        log(str(exc))
        return []
    finally:
        sock.close()
    return []


def read_batteries(debug=None) -> list[Reading]:
    # The running hub already owns the dongle reports and republishes them
    # on the port the web driver uses. None means that port is closed.
    cached = _cached_hub()
    if cached:
        return cached
    hub = _read_hub(debug)
    if hub:
        return hub
    if hub is not None:
        return [Reading(False, "英菲克鼠标", None, None, None, "没有电量回报，动一下鼠标", "INPHIC HUB")]
    receivers = find_receivers()
    if not receivers:
        return []

    def once() -> list[Reading]:
        readings: list[Reading] = []
        for dev in receivers:
            readings.extend(_read_one(dev, debug))
        return readings

    return _locked(once)
