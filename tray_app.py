"""Resident tray icon. Right-click opens the panel, left-click reads once.

The window timer does the waiting. Nothing polls or sleeps while the tray is idle.
Closing the tray posts a quit message so the process does not stay behind.
"""

from __future__ import annotations

import argparse
import ctypes
import queue
import sys
import threading
import time
import traceback
from ctypes import wintypes

import alerts
import delux_battery as battery
import dpi_watch
import devices
import win_icon
import win_setup as setup

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
MSG_TRAY = WM_APP + 1
MSG_DONE = WM_APP + 2
MSG_NOTE = WM_APP + 3
NIF_INFO = 0x10
NIIF_INFO = 0x1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
WS_POPUP = 0x80000000
PANEL_TITLE = "2.4G 设备电量"

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", ctypes.c_void_p),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", ctypes.c_void_p),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", setup.GUID),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hWnd", ctypes.c_void_p),
        ("message", wintypes.UINT),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", wintypes.DWORD),
        ("pt_x", wintypes.LONG),
        ("pt_y", wintypes.LONG),
    ]


user32.RegisterClassExW.restype = wintypes.ATOM
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.CreateWindowExW.restype = ctypes.c_void_p
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
user32.DestroyWindow.argtypes = [ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


class Tray:
    def __init__(self, debug: bool):
        self.debug = debug
        self.hwnd = None
        self.proc = None
        self.busy = False
        self.added = False
        self.hicon = None
        self.readings: list[battery.Reading] = []
        self.ready = False
        self.notes: queue.Queue[tuple[str, str]] = queue.Queue()
        self.interval = setup.load_config()["interval"]
        self.msg_quit = user32.RegisterWindowMessageW(setup.MSG_QUIT)
        self.msg_reload = user32.RegisterWindowMessageW(setup.MSG_RELOAD)
        self.taskbar = user32.RegisterWindowMessageW("TaskbarCreated")

    def log(self, text: str) -> None:
        if self.debug:
            print(time.strftime("%H:%M:%S"), text, flush=True)

    def _nid(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = MSG_TRAY
        return nid

    def _tip(self) -> str:
        stamp = time.strftime("%H:%M:%S")
        if not self.ready:
            return "正在读取…"
        if not self.readings:
            return f"没有已连接的设备\n{stamp}"
        lines = []
        for reading in self.readings:
            state = f"{reading.level}% 充电中" if reading.charging else f"{reading.level}%"
            if reading.dpi_value:
                state += f" · {reading.dpi_value} DPI"
            elif reading.dpi:
                state += f" · DPI 第 {reading.dpi} 档"
            lines.append(f"{reading.name} {state}")
        lines.append(stamp)
        return "\n".join(lines)

    def _icon_level(self) -> int | None:
        levels = [reading.level for reading in self.readings if reading.ok and reading.level is not None]
        if not levels:
            return None
        return min(levels)

    def _show(self, action: int) -> None:
        level = self._icon_level()
        text = str(level) if level is not None else "--"
        icon = win_icon.make_hicon(text, battery.level_rgb(level))
        nid = self._nid()
        nid.hIcon = icon
        nid.szTip = self._tip()[:127]
        ok = bool(shell32.Shell_NotifyIconW(action, ctypes.byref(nid)))
        # The shell keeps using this handle. Destroying it immediately leaves
        # the previous "--" badge on screen.
        previous = self.hicon
        self.hicon = icon
        if previous:
            win_icon.destroy_icon(previous)
        if action == NIM_ADD and not ok:
            # A deleted icon cannot be brought back with NIM_MODIFY.
            self.added = False
        else:
            self.added = True
        self.log(f"icon {action} -> {ok}")

    def add_icon(self) -> None:
        self._show(NIM_ADD)

    def update_icon(self) -> None:
        if not self.added:
            self.add_icon()
            return
        self._show(NIM_MODIFY)

    def show_balloon(self, title: str, text: str) -> None:
        if not self.added or not self.hwnd:
            return
        nid = self._nid()
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_INFO
        nid.hIcon = self.hicon or 0
        nid.szTip = self._tip()[:127]
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def post_note(self, title: str, text: str) -> None:
        self.notes.put((title, text))
        if self.hwnd and user32.IsWindow(self.hwnd):
            user32.PostMessageW(self.hwnd, MSG_NOTE, 0, 0)

    def remove_icon(self) -> None:
        nid = self._nid()
        nid.uFlags = 0
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        if self.hicon:
            win_icon.destroy_icon(self.hicon)
            self.hicon = None
        self.added = False

    def apply_timer(self) -> None:
        user32.KillTimer(self.hwnd, 1)
        user32.SetTimer(self.hwnd, 1, int(self.interval) * 1000, None)

    def reload_config(self) -> None:
        config = setup.load_config()
        interval = config["interval"]
        if interval != self.interval:
            self.interval = interval
            self.log(f"间隔 {interval}s")
            self.apply_timer()
        dpi_watch.configure(bool(config["dpi_alert"]))

    def refresh(self) -> None:
        if self.busy or not self.hwnd:
            return
        self.busy = True
        threading.Thread(target=self._worker, args=(self.hwnd,), daemon=True).start()

    def _worker(self, hwnd) -> None:
        try:
            found = devices.read_devices(self.log if self.debug else None)
            self.readings = devices.connected(found)
            self.ready = True
            alerts.notify(self.readings)
        except Exception:
            self.log(traceback.format_exc())
        if user32.IsWindow(hwnd):
            user32.PostMessageW(hwnd, MSG_DONE, 0, 0)

    def open_panel(self) -> None:
        hwnd = user32.FindWindowW(None, PANEL_TITLE)
        if hwnd:
            setup.foreground_window(hwnd)
            self.log("面板已在前台")
            return
        import subprocess
        subprocess.Popen(
            [setup.pythonw_path(), str(setup.ROOT / "my_devices.py")],
            cwd=str(setup.ROOT),
            creationflags=0x00000008,
        )

    def quit(self) -> None:
        self.log("退出")
        dpi_watch.stop()
        self.remove_icon()
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)

    def wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == MSG_TRAY:
                event = int(lparam) & 0xFFFF
                if event == WM_RBUTTONUP:
                    self.open_panel()
                elif event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.refresh()
                return 0
            if msg == MSG_NOTE:
                while True:
                    try:
                        title, text = self.notes.get_nowait()
                    except queue.Empty:
                        break
                    self.show_balloon(title, text)
                return 0
            if msg == MSG_DONE:
                self.busy = False
                for reading in self.readings:
                    self.log(battery.format_line(reading))
                self.update_icon()
                return 0
            if msg == WM_TIMER:
                self.refresh()
                return 0
            if msg == self.msg_quit:
                self.quit()
                return 0
            if msg == self.msg_reload:
                self.reload_config()
                return 0
            if msg == self.taskbar:
                self.add_icon()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            self.log(traceback.format_exc())
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self) -> int:
        instance = kernel32.GetModuleHandleW(None)
        self.proc = WNDPROC(self.wnd_proc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self.proc
        wc.hInstance = instance
        wc.lpszClassName = setup.WND_CLASS
        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom and ctypes.get_last_error() not in (0, 1410):
            self.log(f"RegisterClassExW {ctypes.get_last_error()}")
            return 1
        self.hwnd = user32.CreateWindowExW(
            0, setup.WND_CLASS, "电量托盘", WS_POPUP, 0, 0, 0, 0, None, None, instance, None
        )
        if not self.hwnd:
            self.log(f"CreateWindowExW {ctypes.get_last_error()}")
            return 1
        setup.promote_tray_icon()
        self.add_icon()
        dpi_watch.start()
        setup.promote_tray_icon()
        self.apply_timer()
        self.refresh()
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self.log("消息循环结束")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="2.4G 电量托盘")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--quit", action="store_true")
    args = parser.parse_args(argv)
    if args.status:
        print("间隔", setup.load_config()["interval"], "秒")
        print("开机自启", "开" if setup.is_autostart_enabled() else "关")
        print("托盘", "在运行" if setup.tray_hwnd() else "没在运行")
        return 0
    if args.quit:
        print("已通知托盘退出" if setup.post_to_tray(setup.MSG_QUIT) else "托盘没在运行")
        return 0
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    mutex = setup.acquire_tray_mutex()
    if mutex is None:
        setup.post_to_tray(setup.MSG_RELOAD)
        print("托盘已经在运行。右键图标打开面板。")
        return 0
    setup.write_pid()
    try:
        return Tray(args.debug).run()
    finally:
        setup.remove_pid()
        kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    sys.exit(main())
