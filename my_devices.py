"""Compact battery panel, plus ``--once`` for a single reading."""

from __future__ import annotations

import argparse
import ctypes
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import delux_battery as battery
import win_setup as setup

PANEL_TITLE = "2.4G 设备电量"
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.restype = ctypes.c_void_p
user32.FindWindowW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def read_once(debug: bool) -> int:
    reading = battery.read_battery(print if debug else None)
    print(battery.format_line(reading))
    if reading.source:
        print(reading.source)
    return 0 if reading.ok else 1


class Panel:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(PANEL_TITLE)
        self.root.configure(bg="#eef1f4")
        self.root.geometry("480x560")
        self.root.minsize(440, 500)
        self.queue: queue.Queue[battery.Reading] = queue.Queue()
        self.busy = False
        self.syncing = False
        self.alive = True
        self._timer = None
        self.reading = battery.Reading(False, "多彩 M900 Mini Pro+", None, None, None, "正在读取…", "")
        self.interval = tk.IntVar(value=setup.load_config()["interval"])
        self.autostart = tk.BooleanVar(value=setup.is_autostart_enabled())
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(80, self._pump)
        self.root.after(1500, self._sync_autostart)
        self.refresh()
        self._arm()

    def _build(self) -> None:
        font = "Microsoft YaHei UI"
        wrap = tk.Frame(self.root, bg="#eef1f4")
        wrap.pack(fill="both", expand=True, padx=18, pady=16)
        tk.Label(wrap, text="2.4G 设备电量", bg="#eef1f4", fg="#1c2430",
                 font=(font, 16, "bold")).pack(anchor="w")
        tk.Label(wrap, text="多彩 M900 Mini Pro+  ·  1K 接收器", bg="#eef1f4",
                 fg="#5c6b7a", font=(font, 9)).pack(anchor="w", pady=(2, 12))

        self.card = tk.Frame(wrap, bg="white", highlightthickness=1, highlightbackground="#d5dbe3")
        self.card.pack(fill="x")
        inner = tk.Frame(self.card, bg="white")
        inner.pack(fill="x", padx=20, pady=16)
        self.inner = inner
        self.name = tk.Label(inner, text=self.reading.name, bg="white", fg="#1c2430",
                             font=(font, 13, "bold"), anchor="w", justify="left")
        self.name.pack(anchor="w", fill="x")
        self.percent = tk.Label(inner, text="--%", bg="white", fg="#96a0aa",
                                font=(font, 42, "bold"), anchor="w", justify="left")
        self.percent.pack(anchor="w", fill="x", pady=(4, 0))
        self.bar = tk.Canvas(inner, height=12, bg="#e6ebf0", highlightthickness=0)
        self.bar.pack(fill="x", pady=(8, 8))
        self.detail = tk.Label(inner, text="正在读取…", bg="white", fg="#3d4c5c",
                               font=(font, 10), anchor="w", justify="left")
        self.detail.pack(anchor="w", fill="x")
        self.source = tk.Label(inner, text="", bg="white", fg="#7b8794",
                               font=(font, 9), anchor="w", justify="left")
        self.source.pack(anchor="w", fill="x", pady=(6, 0))
        inner.bind("<Configure>", self._fit_text)

        options = tk.Frame(wrap, bg="#eef1f4")
        options.pack(fill="x", pady=(16, 8))
        tk.Label(options, text="刷新间隔", bg="#eef1f4", fg="#1c2430", font=(font, 10)).pack(side="left")
        labels = {30: "30 秒", 60: "1 分钟", 120: "2 分钟", 300: "5 分钟", 600: "10 分钟", 1800: "30 分钟"}
        self.labels = labels
        self.box = ttk.Combobox(options, state="readonly", width=12,
                                values=list(labels.values()))
        self.box.set(labels.get(self.interval.get(), "1 分钟"))
        self.box.pack(side="left", padx=(8, 0))
        self.box.bind("<<ComboboxSelected>>", self._on_interval)

        tk.Checkbutton(
            wrap, text="开机自启",
            variable=self.autostart, command=self._on_autostart,
            bg="#eef1f4", fg="#1c2430", activebackground="#eef1f4",
            font=(font, 10), anchor="w",
        ).pack(fill="x", pady=(4, 0))
        tk.Label(
            wrap, text="对应任务管理器「启动」里的「电量托盘」，可以在那里直接禁用。",
            bg="#eef1f4", fg="#5c6b7a", font=(font, 8), anchor="w", justify="left", wraplength=420,
        ).pack(fill="x", pady=(0, 10))

        buttons = tk.Frame(wrap, bg="#eef1f4")
        buttons.pack(fill="x")
        self._button(buttons, "立即刷新", self.refresh).pack(side="left")
        self._button(buttons, "打开托盘", self.open_tray).pack(side="left", padx=8)
        self._button(buttons, "退出托盘", self.quit_tray).pack(side="left")
        self._button(wrap, "放到桌面", self.make_links).pack(anchor="w", pady=(10, 0))
        self.status = tk.Label(wrap, text="", bg="#eef1f4", fg="#5c6b7a",
                               font=(font, 8), wraplength=360, justify="left")
        self.status.pack(anchor="w", pady=(12, 0))

    def _fit_text(self, _event=None) -> None:
        width = max(self.inner.winfo_width() - 4, 180)
        self.detail.configure(wraplength=width)
        self.source.configure(wraplength=width)

    def _button(self, parent, text, command):
        return tk.Button(
            parent, text=text, command=command, relief="flat",
            bg="#1f6feb", fg="white", activebackground="#1a5fd0", activeforeground="white",
            font=("Microsoft YaHei UI", 9), padx=12, pady=6, cursor="hand2",
        )

    def _on_interval(self, _event=None) -> None:
        label = self.box.get()
        for seconds, text in self.labels.items():
            if text == label:
                self.interval.set(seconds)
                break
        setup.save_config({"interval": self.interval.get()})
        setup.post_to_tray(setup.MSG_RELOAD)
        self._arm()
        self.status.configure(text=f"刷新间隔已改为 {label}。托盘会在下一轮使用这个间隔。")

    def _on_autostart(self) -> None:
        if self.syncing:
            return
        try:
            setup.set_autostart(bool(self.autostart.get()))
            state = "已加入启动文件夹" if self.autostart.get() else "已在任务管理器里禁用"
            self.status.configure(text=f"开机自启{state}。")
        except OSError as exc:
            self.syncing = True
            self.autostart.set(setup.is_autostart_enabled())
            self.syncing = False
            self.status.configure(text=f"没能改开机自启：{exc}")

    def _sync_autostart(self) -> None:
        if not self.alive:
            return
        actual = setup.is_autostart_enabled()
        if actual != bool(self.autostart.get()):
            self.syncing = True
            self.autostart.set(actual)
            self.syncing = False
        self.root.after(2000, self._sync_autostart)

    def _arm(self) -> None:
        if not self.alive:
            return
        if self._timer is not None:
            self.root.after_cancel(self._timer)
        self._timer = self.root.after(self.interval.get() * 1000, self._tick)

    def _tick(self) -> None:
        if not self.alive:
            return
        self.refresh()
        self._arm()

    def refresh(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.status.configure(text="正在读接收器…")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        try:
            reading = battery.read_battery()
        except Exception as exc:
            reading = battery.Reading(False, "多彩 M900 Mini Pro+", None, None, None, f"读取失败：{exc}", "")
        self.queue.put(reading)

    def _pump(self) -> None:
        if not self.alive:
            return
        try:
            while True:
                self._show(self.queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _show(self, reading: battery.Reading) -> None:
        self.busy = False
        self.reading = reading
        color = battery.level_hex(reading.level if reading.ok else None)
        self.name.configure(text=reading.name)
        self.percent.configure(text=f"{reading.level}%" if reading.ok else "--%", fg=color)
        self.detail.configure(text=reading.detail)
        ident = ""
        if reading.cid is not None:
            ident = f"cid {reading.cid} · mid {reading.mid}"
        self.source.configure(text="  ·  ".join(part for part in (reading.source, ident, time.strftime("%H:%M:%S")) if part))
        self.bar.delete("all")
        width = max(self.bar.winfo_width(), 340)
        self.bar.create_rectangle(0, 0, width, 10, fill="#e6ebf0", outline="")
        if reading.ok and reading.level is not None:
            self.bar.create_rectangle(0, 0, width * reading.level / 100, 10, fill=color, outline="")
        self.status.configure(text="")

    def open_tray(self) -> None:
        if setup.tray_hwnd():
            self.status.configure(text="托盘已经在运行。左键图标立刻刷新，右键打开这个面板。")
            return
        subprocess.Popen(
            [setup.pythonw_path(), str(setup.launcher_path())],
            cwd=str(setup.ROOT),
            creationflags=0x00000008,
        )
        self.status.configure(text="托盘已启动。图标是电量数字，右键可以再打开这个面板。")

    def quit_tray(self) -> None:
        if setup.post_to_tray(setup.MSG_QUIT):
            self.status.configure(text="已让托盘退出。")
        else:
            self.status.configure(text="托盘没在运行。")

    def make_links(self) -> None:
        try:
            self.status.configure(text=setup.create_desktop_entries())
        except OSError as exc:
            self.status.configure(text=f"没能放到桌面：{exc}")

    def close(self) -> None:
        self.alive = False
        if self._timer is not None:
            self.root.after_cancel(self._timer)
        self.root.destroy()

    def run(self) -> int:
        self.root.mainloop()
        return 0


def _raise_existing_panel() -> bool:
    hwnd = user32.FindWindowW(None, PANEL_TITLE)
    if not hwnd:
        return False
    setup.foreground_window(hwnd)
    return True


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    parser = argparse.ArgumentParser(description="2.4G 设备电量")
    parser.add_argument("--once", action="store_true", help="读一次电量后退出")
    parser.add_argument("--debug", action="store_true", help="--once 时打印报文")
    parser.add_argument("--links", action="store_true", help="在桌面放项目目录和启动文件的链接")
    args = parser.parse_args(argv)
    if args.links:
        print(setup.create_desktop_entries())
        return 0
    if args.once:
        return read_once(args.debug)
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    if _raise_existing_panel():
        print("面板已经打开。")
        return 0
    return Panel().run()


if __name__ == "__main__":
    sys.exit(main())
