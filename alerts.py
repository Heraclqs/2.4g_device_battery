"""Windows toast when battery is low or a mouse DPI gear changes.

State lives next to the other app settings so a restart does not repeat the
same warning. The tray and the panel can both call ``notify``; a mutex keeps
a single toast.
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import threading
import time

import win_setup as setup
from delux_battery import Reading

STATE_PATH = setup.APP_DIR / "alert_state.json"
LIVE_PATH = setup.APP_DIR / "dpi_live.json"
LOCK_NAME = "Local\\BatteryAlert24G"
LOW_STEP = 5

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
kernel32.ReleaseMutex.argtypes = [ctypes.wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]


def _key(reading: Reading) -> str:
    return f"{reading.vid:04X}:{reading.pid:04X}:{reading.name}"


def _dpi_key(reading: Reading) -> str:
    return f"{reading.vid:04X}:{reading.pid:04X}"


def _load_state() -> dict:
    try:
        found = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        found = {}
    if not isinstance(found, dict):
        found = {}
    low = found.get("low")
    dpi = found.get("dpi")
    return {
        "low": low if isinstance(low, dict) else {},
        "dpi": dpi if isinstance(dpi, dict) else {},
    }


def _save_state(state: dict) -> None:
    setup.APP_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_popup_lock = threading.Lock()
_popup_latest: dict[str, tuple[str, str]] = {}
_popup_event = threading.Event()
_popup_thread: threading.Thread | None = None
_toast_proc: subprocess.Popen | None = None

_TOAST_HOST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Microsoft.Windows.Explorer")
$utf8 = New-Object System.Text.UTF8Encoding $false
[Console]::InputEncoding = $utf8
$reader = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), $utf8)
function Esc([string]$s) {
  if ($null -eq $s) { return "" }
  return $s.Replace('&','&amp;').Replace('<','&lt;').Replace('>','&gt;').Replace('"','&quot;')
}
while ($null -ne ($line = $reader.ReadLine())) {
  $parts = $line.Split("`t", 4)
  if ($parts.Length -lt 4) { continue }
  $tag, $title, $message, $silent = $parts
  $audio = ""
  if ($silent -eq "1") { $audio = "<audio silent='true'/>" }
  $doc = "<toast>" + $audio + "<visual><binding template='ToastGeneric'><text>" + (Esc $title) + "</text><text>" + (Esc $message) + "</text></binding></visual></toast>"
  $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
  $xml.LoadXml($doc)
  $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
  $toast.Tag = $tag
  $toast.Group = "24g-battery"
  $notifier.Show($toast)
}
"""


def _toast_host() -> subprocess.Popen:
    global _toast_proc
    if _toast_proc is not None and _toast_proc.poll() is None and _toast_proc.stdin is not None:
        return _toast_proc
    setup.APP_DIR.mkdir(parents=True, exist_ok=True)
    path = setup.APP_DIR / "toast_host.ps1"
    path.write_text(_TOAST_HOST, encoding="utf-8-sig")
    _toast_proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0x08000000,
    )
    return _toast_proc


def warm_toast() -> None:
    _toast_host()


def _popup_worker() -> None:
    global _toast_proc
    while True:
        _popup_event.wait()
        _popup_event.clear()
        with _popup_lock:
            batch = list(_popup_latest.items())
            _popup_latest.clear()
        if not batch:
            continue
        try:
            host = _toast_host()
            stdin = host.stdin
            if stdin is None:
                continue
            for tag, (title, message) in batch:
                silent = "1" if tag == "dpi" else "0"
                title = title.replace("\t", " ").replace("\n", " ")
                message = message.replace("\t", " ").replace("\n", " ")
                stdin.write(f"{tag}\t{title}\t{message}\t{silent}\n".encode("utf-8"))
            stdin.flush()
        except (OSError, ValueError):
            _toast_proc = None
        with _popup_lock:
            if _popup_latest:
                _popup_event.set()


def windows_popup(title: str, message: str, tag: str = "notice") -> None:
    """Show the newest message for this tag. Older ones still waiting are dropped."""
    global _popup_thread
    with _popup_lock:
        _popup_latest[tag] = (title, message)
        if _popup_thread is None or not _popup_thread.is_alive():
            _popup_thread = threading.Thread(target=_popup_worker, name="toast", daemon=True)
            _popup_thread.start()
    _popup_event.set()


def _dpi_message(reading: Reading) -> str:
    if reading.dpi_value:
        return f"{reading.name} 已切换到第 {reading.dpi} 档，{reading.dpi_value} DPI"
    return f"{reading.name} 已切换到第 {reading.dpi} 档"


def _publish_live(readings: list[Reading]) -> None:
    data: dict = {}
    try:
        found = json.loads(LIVE_PATH.read_text(encoding="utf-8"))
        if isinstance(found, dict):
            data = found
    except (OSError, json.JSONDecodeError):
        data = {}
    changed = False
    for reading in readings:
        if not reading.dpi:
            continue
        payload = {
            "name": reading.name,
            "gear": reading.dpi,
            "value": reading.dpi_value,
        }
        key = _dpi_key(reading)
        if data.get(key) != payload:
            data[key] = payload
            changed = True
    if not changed:
        return
    setup.APP_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _plan(readings: list[Reading]) -> list[tuple[str, str]]:
    config = setup.load_config()
    state = _load_state()
    seen: set[str] = set()
    lows: list[str] = []
    dpis: list[str] = []
    threshold = int(config["low_percent"])
    for reading in readings:
        if not reading.ok or reading.level is None:
            continue
        key = _key(reading)
        seen.add(key)
        low_now = (
            config["low_battery"]
            and not reading.charging
            and reading.level <= threshold
        )
        if low_now:
            previous = state["low"].get(key)
            if previous is None or reading.level <= int(previous) - LOW_STEP:
                lows.append(f"{reading.name} 还剩 {reading.level}%")
                state["low"][key] = reading.level
        else:
            state["low"].pop(key, None)
        if reading.dpi:
            state["dpi"][_dpi_key(reading)] = reading.dpi
    for key in list(state["low"]):
        if key not in seen:
            state["low"].pop(key, None)
    _save_state(state)
    _publish_live(readings)
    notes: list[tuple[str, str]] = [("low", text) for text in lows]
    notes.extend(("dpi", text) for text in dpis)
    return notes


def notify(readings: list[Reading]) -> None:
    mutex = kernel32.CreateMutexW(None, False, LOCK_NAME)
    if mutex:
        waited = kernel32.WaitForSingleObject(mutex, 3000)
        if waited not in (0, 0x80):
            kernel32.CloseHandle(mutex)
            return
    try:
        notes = _plan(readings)
    finally:
        if mutex:
            kernel32.ReleaseMutex(mutex)
            kernel32.CloseHandle(mutex)
    lows = [text for kind, text in notes if kind == "low"]
    if lows:
        windows_popup("电量偏低", "；".join(lows))


_dpi_mem: dict[str, int] = {}
_live_mem: dict[str, dict] = {}
_mem_ready = False
_disk_lock = threading.Lock()
_disk_pending: tuple[dict, dict] | None = None
_disk_event = threading.Event()
_disk_thread: threading.Thread | None = None


def _remember_disk() -> None:
    global _mem_ready
    if _mem_ready:
        return
    state = _load_state()
    for key, gear in state["dpi"].items():
        try:
            _dpi_mem[str(key)] = int(gear)
        except (TypeError, ValueError):
            continue
    try:
        found = json.loads(LIVE_PATH.read_text(encoding="utf-8"))
        if isinstance(found, dict):
            _live_mem.update(found)
    except (OSError, json.JSONDecodeError):
        pass
    _mem_ready = True


def _disk_worker() -> None:
    global _disk_pending
    while True:
        _disk_event.wait()
        _disk_event.clear()
        with _disk_lock:
            pending = _disk_pending
            _disk_pending = None
        if pending is None:
            continue
        live, dpi_map = pending
        state = _load_state()
        state["dpi"] = dpi_map
        _save_state(state)
        setup.APP_DIR.mkdir(parents=True, exist_ok=True)
        LIVE_PATH.write_text(json.dumps(live, ensure_ascii=False), encoding="utf-8")


def _queue_disk() -> None:
    global _disk_thread, _disk_pending
    with _disk_lock:
        _disk_pending = (dict(_live_mem), dict(_dpi_mem))
        if _disk_thread is None or not _disk_thread.is_alive():
            _disk_thread = threading.Thread(target=_disk_worker, name="dpi-disk", daemon=True)
            _disk_thread.start()
    _disk_event.set()


def note_dpi(readings: list[Reading], popup=windows_popup, announce: bool = False) -> None:
    """Record a gear change without waiting on disk.

    The first sample after startup only sets the baseline. A popup means the
    gear changed while this process was already watching.
    """
    if not readings:
        return
    messages = []
    for reading in readings:
        if not reading.dpi:
            continue
        key = _dpi_key(reading)
        previous = _dpi_mem.get(key)
        if previous is not None and previous != reading.dpi:
            messages.append(_dpi_message(reading))
        elif announce and previous is None:
            messages.append(_dpi_message(reading))
        _dpi_mem[key] = reading.dpi
        _live_mem[key] = {
            "name": reading.name,
            "gear": reading.dpi,
            "value": reading.dpi_value,
        }
    _queue_disk()
    if messages and popup:
        popup("DPI 已切换", "；".join(messages), tag="dpi")


def notify_dpi(readings: list[Reading], popup=windows_popup) -> None:
    """Compare gears only. A live watcher calls this without a battery level."""
    if not readings or not setup.load_config()["dpi_alert"]:
        return
    mutex = kernel32.CreateMutexW(None, False, LOCK_NAME)
    if mutex:
        waited = kernel32.WaitForSingleObject(mutex, 1000)
        if waited not in (0, 0x80):
            kernel32.CloseHandle(mutex)
            return
    try:
        state = _load_state()
        messages = []
        for reading in readings:
            if not reading.dpi:
                continue
            dpi_key = _dpi_key(reading)
            previous = state["dpi"].get(dpi_key)
            if previous is not None and int(previous) != reading.dpi:
                messages.append(_dpi_message(reading))
            state["dpi"][dpi_key] = reading.dpi
        _save_state(state)
        _publish_live(readings)
    finally:
        if mutex:
            kernel32.ReleaseMutex(mutex)
            kernel32.CloseHandle(mutex)
    if messages and popup:
        popup("DPI 已切换", "；".join(messages), tag="dpi")
