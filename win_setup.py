"""Startup shortcut, desktop links, and the single-instance tray window.

The startup entry is a ``.lnk`` in the Startup folder, so Task Manager's
Startup tab can enable or disable it. The tray never creates or deletes that
shortcut; only the panel checkbox does.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_DIR = Path(os.environ.get("APPDATA", str(ROOT))) / "2.4gDeviceBattery"
CONFIG_PATH = APP_DIR / "config.json"
PID_PATH = APP_DIR / "tray.pid"
STARTUP_NAME = "电量托盘.lnk"
DESKTOP_DIR_NAME = "2.4G电量"
DESKTOP_FILE_NAME = "启动电量托盘.pyw"
WND_CLASS = "BatteryTray24G"
MUTEX_NAME = "Local\\BatteryTray24G"
MSG_QUIT = "2.4gBattery.Quit"
MSG_RELOAD = "2.4gBattery.Reload"
INTERVAL_CHOICES = (30, 60, 120, 300, 600, 1800)

ole32 = ctypes.WinDLL("ole32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

CSIDL_STARTUP = 7
CSIDL_DESKTOPDIRECTORY = 0x10
CLSCTX_INPROC_SERVER = 1
SW_RESTORE = 9
ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001

# IShellLinkW vtable, including IUnknown: SetPath=20, SetArguments=11,
# SetWorkingDirectory=9, SetDescription=7, GetPath=3, GetArguments=10.
# IPersistFile: Load=5, Save=6.


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> GUID:
    raw = text.strip("{}")
    a, b, c, rest = raw.split("-", 3)
    d, e = rest.split("-", 1)
    guid = GUID()
    guid.Data1 = int(a, 16)
    guid.Data2 = int(b, 16)
    guid.Data3 = int(c, 16)
    guid.Data4[:] = bytes.fromhex(d + e)
    return guid


CLSID_SHELLLINK = _guid("00021401-0000-0000-C000-000000000046")
IID_SHELLLINKW = _guid("000214F9-0000-0000-C000-000000000046")
IID_PERSISTFILE = _guid("0000010B-0000-0000-C000-000000000046")

ole32.CoInitialize.argtypes = [ctypes.c_void_p]
ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoCreateInstance.restype = ctypes.c_long
shell32.SHGetFolderPathW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_wchar_p]
user32.FindWindowW.restype = ctypes.c_void_p
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.IsWindow.argtypes = [ctypes.c_void_p]
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateSymbolicLinkW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
kernel32.CreateSymbolicLinkW.restype = wintypes.BOOLEAN
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]


def _com(unk, index, restype, argtypes, *args):
    table = ctypes.cast(ctypes.cast(unk, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    func = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table[index])
    return func(unk, *args)


def _release(unk) -> None:
    if unk:
        _com(unk, 2, ctypes.c_ulong, [])


def folder(csidl: int) -> Path:
    buf = ctypes.create_unicode_buffer(260)
    if shell32.SHGetFolderPathW(None, csidl, None, 0, buf) != 0:
        raise OSError(f"SHGetFolderPathW {csidl} failed")
    return Path(buf.value)


def pythonw_path() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        return str(exe)
    sibling = exe.with_name("pythonw.exe")
    if sibling.exists():
        return str(sibling)
    return str(exe)


def launcher_path() -> Path:
    return ROOT / "start_tray.pyw"


DEFAULT_CONFIG = {
    "interval": 60,
    "low_battery": True,
    "low_percent": 20,
    "dpi_alert": True,
}


def _normalize_config(data: dict) -> dict:
    merged = dict(DEFAULT_CONFIG)
    if isinstance(data, dict):
        merged.update(data)
    try:
        interval = int(merged.get("interval", 60))
    except (TypeError, ValueError):
        interval = 60
    try:
        percent = int(merged.get("low_percent", 20))
    except (TypeError, ValueError):
        percent = 20
    return {
        "interval": min(1800, max(30, interval)),
        "low_battery": bool(merged.get("low_battery", True)),
        "low_percent": min(50, max(5, percent)),
        "dpi_alert": bool(merged.get("dpi_alert", True)),
    }


def load_config() -> dict:
    data: dict = {}
    try:
        found = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(found, dict):
            data = found
    except (OSError, json.JSONDecodeError):
        pass
    return _normalize_config(data)


def save_config(data: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    payload = _normalize_config({**load_config(), **data})
    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _startup_approved_key():
    import winreg
    return winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder")


def _filetime_now() -> bytes:
    class FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
    ft = FILETIME()
    kernel32.GetSystemTimeAsFileTime(ctypes.byref(ft))
    return bytes(ctypes.string_at(ctypes.addressof(ft), 8))


def startup_lnk() -> Path:
    return folder(CSIDL_STARTUP) / STARTUP_NAME


def _approved_flag() -> int | None:
    """2 enabled, 3 disabled, None when Task Manager has no opinion yet."""
    import winreg
    try:
        key = _startup_approved_key()
        raw, _ = winreg.QueryValueEx(key, STARTUP_NAME)
        winreg.CloseKey(key)
    except OSError:
        return None
    blob = bytes(raw)
    if not blob:
        return None
    return blob[0]


def set_startup_approved(enabled: bool) -> None:
    import winreg
    flag = 2 if enabled else 3
    blob = bytes([flag, 0, 0, 0]) + _filetime_now()
    key = _startup_approved_key()
    try:
        winreg.SetValueEx(key, STARTUP_NAME, 0, winreg.REG_BINARY, blob)
    finally:
        winreg.CloseKey(key)


def create_shortcut(path: Path, target: str, arguments: str, workdir: str, description: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    hr = ole32.CoInitialize(None)
    if hr < 0:
        raise OSError(f"CoInitialize {hr:#x}")
    link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_SHELLLINK), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(IID_SHELLLINKW), ctypes.byref(link),
        )
        if hr < 0 or not link.value:
            raise OSError(f"CoCreateInstance {hr:#x}")
        def call(index, *args, types):
            result = _com(link, index, ctypes.c_long, types, *args)
            if result < 0:
                raise OSError(f"IShellLink[{index}] {result:#x}")
        call(20, target, types=[wintypes.LPCWSTR])
        call(11, arguments, types=[wintypes.LPCWSTR])
        call(9, workdir, types=[wintypes.LPCWSTR])
        call(7, description, types=[wintypes.LPCWSTR])
        hr = _com(link, 0, ctypes.c_long, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                  ctypes.byref(IID_PERSISTFILE), ctypes.byref(persist))
        if hr < 0 or not persist.value:
            raise OSError(f"QueryInterface IPersistFile {hr:#x}")
        hr = _com(persist, 6, ctypes.c_long, [wintypes.LPCWSTR, wintypes.BOOL], str(path), True)
        if hr < 0:
            raise OSError(f"IPersistFile::Save {hr:#x}")
    finally:
        _release(persist)
        _release(link)
        ole32.CoUninitialize()


def read_shortcut(path: Path) -> tuple[str, str]:
    ole32.CoInitialize(None)
    link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_SHELLLINK), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(IID_SHELLLINKW), ctypes.byref(link),
        )
        if hr < 0:
            raise OSError(f"CoCreateInstance {hr:#x}")
        hr = _com(link, 0, ctypes.c_long, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                  ctypes.byref(IID_PERSISTFILE), ctypes.byref(persist))
        if hr < 0:
            raise OSError(f"QueryInterface {hr:#x}")
        hr = _com(persist, 5, ctypes.c_long, [wintypes.LPCWSTR, wintypes.DWORD], str(path), 0)
        if hr < 0:
            raise OSError(f"Load {hr:#x}")
        target = ctypes.create_unicode_buffer(1024)
        args = ctypes.create_unicode_buffer(1024)
        _com(link, 3, ctypes.c_long, [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
             target, 1024, None, 0)
        _com(link, 10, ctypes.c_long, [ctypes.c_wchar_p, ctypes.c_int], args, 1024)
        return target.value, args.value
    finally:
        _release(persist)
        _release(link)
        ole32.CoUninitialize()


def is_autostart_enabled() -> bool:
    link = startup_lnk()
    if not link.exists():
        return False
    flag = _approved_flag()
    return flag != 3


def set_autostart(enabled: bool) -> None:
    link = startup_lnk()
    if enabled:
        create_shortcut(
            link,
            pythonw_path(),
            f'"{launcher_path()}"',
            str(ROOT),
            "电量托盘",
        )
        set_startup_approved(True)
    else:
        if link.exists():
            set_startup_approved(False)


def _same_target(link: Path, target: Path) -> bool:
    try:
        if link.is_symlink():
            return link.resolve() == target.resolve()
        return link.exists() and os.path.samefile(link, target)
    except OSError:
        return False


def _link_file(link: Path, target: Path) -> str:
    if link.exists() or link.is_symlink():
        if _same_target(link, target):
            return "已存在"
        raise OSError(f"桌面上的 {link.name} 已经指向别的文件")
    flags = 0x2  # SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE
    if kernel32.CreateSymbolicLinkW(str(link), str(target), flags):
        return "符号链接"
    if link.drive.lower() == target.drive.lower():
        try:
            os.link(target, link)
            return "硬链接"
        except OSError:
            pass
    create_shortcut(link.with_suffix(".lnk"), pythonw_path(), f'"{target}"', str(target.parent), "电量托盘")
    return "快捷方式（符号链接未获授权，且跨盘符不能做硬链接）"


def _link_dir(link: Path, target: Path) -> str:
    if link.exists():
        return "已存在"
    flags = 0x1 | 0x2
    if kernel32.CreateSymbolicLinkW(str(link), str(target), flags):
        return "符号链接"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if completed.returncode == 0:
        return "目录联接"
    raise OSError(completed.stderr.strip() or completed.stdout.strip() or "mklink 失败")


def create_desktop_entries() -> str:
    desktop = folder(CSIDL_DESKTOPDIRECTORY)
    kind_dir = _link_dir(desktop / DESKTOP_DIR_NAME, ROOT)
    kind_file = _link_file(desktop / DESKTOP_FILE_NAME, launcher_path())
    return f"目录：{kind_dir}；启动文件：{kind_file}"


def promote_tray_icon() -> None:
    """Ask Windows to show this program's notification icon, not only the overflow."""
    import winreg
    exe = os.path.normcase(sys.executable)
    try:
        root = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Control Panel\NotifyIconSettings", 0, winreg.KEY_READ
        )
    except OSError:
        return
    index = 0
    try:
        while True:
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                sub = winreg.OpenKey(root, name, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
            except OSError:
                continue
            try:
                path, _ = winreg.QueryValueEx(sub, "ExecutablePath")
                if os.path.normcase(str(path)) == exe:
                    winreg.SetValueEx(sub, "IsPromoted", 0, winreg.REG_DWORD, 1)
            except OSError:
                pass
            finally:
                winreg.CloseKey(sub)
    finally:
        winreg.CloseKey(root)


def tray_hwnd():
    return user32.FindWindowW(WND_CLASS, None)


def post_to_tray(message_name: str) -> bool:
    hwnd = tray_hwnd()
    if not hwnd:
        return False
    msg = user32.RegisterWindowMessageW(message_name)
    return bool(user32.PostMessageW(hwnd, msg, 0, 0))


def foreground_window(hwnd) -> None:
    if hwnd and user32.IsWindow(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)


def write_pid() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")


def remove_pid() -> None:
    try:
        PID_PATH.unlink()
    except OSError:
        pass


def _image_of(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def cleanup_zombie() -> bool:
    """A previous tray can keep the mutex after its window is gone. Stop that one."""
    if tray_hwnd():
        return False
    try:
        pid = int(json.loads(PID_PATH.read_text(encoding="utf-8")).get("pid", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    image = _image_of(pid).lower()
    if not image.endswith(("python.exe", "pythonw.exe")):
        return False
    handle = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        kernel32.TerminateProcess(handle, 0)
    finally:
        kernel32.CloseHandle(handle)
    remove_pid()
    return True


def acquire_tray_mutex():
    """Return the mutex handle, or None when a live tray already owns it."""
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
        return handle
    if tray_hwnd():
        return None
    cleanup_zombie()
    # The abandoned mutex is held by the handle we just opened once the zombie dies.
    return handle
