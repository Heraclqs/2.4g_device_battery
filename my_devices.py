"""Home page and settings for the 2.4G battery panel."""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk

import alerts
import delux_battery as battery
import dpi_watch
import product_photo
import devices
import win_setup as setup

PANEL_TITLE = "2.4G 设备电量"
FONT = "Microsoft YaHei UI"
BG = "#faf9f5"
SIDE = "#f3f1ea"
INK = "#1c1b17"
MUTED = "#6f6b63"
LINE = "#e6e2d6"
WHITE = "#ffffff"
ACCENT = "#c96442"
SOFT = "#f6ebe6"
CHIP = "#f4f1ea"
LOW = "#b42318"
MID = "#9a6700"
OK = "#2f6f4e"
IMAGE_SLOT = 210

INTERVALS = (
    (30, "30 秒"),
    (60, "1 分钟"),
    (120, "2 分钟"),
    (300, "5 分钟"),
    (600, "10 分钟"),
    (1800, "30 分钟"),
)
LOW_PERCENTS = (10, 15, 20, 25, 30)

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.restype = ctypes.c_void_p
user32.FindWindowW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _dpi_caption(reading: battery.Reading) -> str:
    if reading.dpi and reading.dpi_value:
        return f"{reading.dpi_value} DPI · 第 {reading.dpi} 档"
    if reading.dpi:
        return f"DPI 第 {reading.dpi} 档"
    return ""


def _tone(level: int | None, threshold: int) -> str:
    if level is None:
        return MUTED
    if level <= threshold:
        return LOW
    if level < 60:
        return MID
    return OK


def read_once(debug: bool) -> int:
    readings = devices.read_devices(print if debug else None)
    online = devices.connected(readings)
    hidden = len(readings) - len(online)
    if not online:
        print("没有已连接的鼠标或键盘")
        if hidden:
            print(f"{hidden} 个接收器插着，但鼠标或键盘没有连上，主页不显示")
        return 1
    for reading in online:
        print(battery.format_line(reading))
        if reading.source:
            print(reading.source)
    if hidden:
        print(f"另有 {hidden} 个接收器没有连上外设，主页已隐藏")
    return 0


class Switch(tk.Canvas):
    def __init__(self, parent, variable: tk.BooleanVar, command, bg: str):
        super().__init__(parent, width=44, height=26, bg=bg, highlightthickness=0, cursor="hand2")
        self.variable = variable
        self.command = command
        self._bg = bg
        self.bind("<Button-1>", self._toggle)
        self.redraw()

    def _toggle(self, _event=None) -> None:
        self.variable.set(not bool(self.variable.get()))
        self.redraw()
        if self.command:
            self.command()

    def redraw(self) -> None:
        self.delete("all")
        on = bool(self.variable.get())
        self.create_oval(1, 3, 43, 23, fill=ACCENT if on else "#ddd6c8", outline="")
        knob = 22 if on else 2
        self.create_oval(knob, 2, knob + 20, 22, fill=WHITE, outline="")


class Panel:
    def __init__(self):
        config = setup.load_config()
        self.root = tk.Tk()
        self.root.title(PANEL_TITLE)
        self.root.configure(bg=BG)
        self.root.geometry("980x720")
        self.root.minsize(860, 600)
        self.queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.busy = False
        self.syncing = False
        self.alive = True
        self.loading = True
        self.page = "home"
        self.online: list[battery.Reading] = []
        self.photos: dict[str, tk.PhotoImage] = {}
        self._dpi_labels: dict[str, tuple[tk.Label, str]] = {}
        self._dpi_known: dict[str, tuple[int, int | None]] = {}
        self._cards: dict[str, dict] = {}
        self._empty: tk.Frame | None = None
        self._timer = None
        self.interval = tk.IntVar(value=int(config["interval"]))
        self.autostart = tk.BooleanVar(value=setup.is_autostart_enabled())
        self.low_battery = tk.BooleanVar(value=bool(config["low_battery"]))
        self.low_percent = tk.IntVar(value=int(config["low_percent"]))
        self.dpi_alert = tk.BooleanVar(value=bool(config["dpi_alert"]))
        self._nav: dict[str, tuple[tk.Frame, tk.Frame, tk.Label]] = {}
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(80, self._pump)
        self.root.after(1500, self._sync_autostart)
        self.refresh()
        self._arm()
        self._dpi_mtime = 0
        dpi_watch.start(yield_to_tray=True)
        self.root.after(40, self._sync_live_dpi)

    def _build(self) -> None:
        sidebar = tk.Frame(self.root, bg=SIDE, width=208)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=SIDE)
        brand.pack(fill="x", padx=22, pady=(26, 8))
        tk.Label(brand, text="电量", bg=SIDE, fg=INK, font=(FONT, 22)).pack(anchor="w")
        tk.Label(brand, text="2.4G 外设", bg=SIDE, fg=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(2, 0))
        nav = tk.Frame(sidebar, bg=SIDE)
        nav.pack(fill="x", padx=14, pady=(22, 0))
        self._nav_button(nav, "home", "主页")
        self._nav_button(nav, "settings", "设置")
        tk.Label(
            sidebar,
            text="只显示已经连上、并回了电量的鼠标和键盘。",
            bg=SIDE, fg=MUTED, font=(FONT, 8), wraplength=164, justify="left",
        ).pack(side="bottom", anchor="w", padx=22, pady=22)

        self.main = tk.Frame(self.root, bg=BG)
        self.main.pack(side="left", fill="both", expand=True)
        self.home = tk.Frame(self.main, bg=BG)
        self.settings = tk.Frame(self.main, bg=BG)
        self._build_home()
        self._build_settings()
        self._show_page("home")

    def _nav_button(self, parent, key: str, text: str) -> None:
        row = tk.Frame(parent, bg=SIDE, cursor="hand2")
        row.pack(fill="x", pady=2)
        mark = tk.Frame(row, bg=SIDE, width=3)
        mark.pack(side="left", fill="y")
        label = tk.Label(row, text=text, bg=SIDE, fg=INK, font=(FONT, 11), anchor="w", padx=14, pady=8)
        label.pack(side="left", fill="x", expand=True)
        for widget in (row, label):
            widget.bind("<Button-1>", lambda _event, name=key: self._show_page(name))
        self._nav[key] = (row, mark, label)

    def _paint_nav(self) -> None:
        for key, (row, mark, label) in self._nav.items():
            selected = key == self.page
            bg = WHITE if selected else SIDE
            row.configure(bg=bg)
            label.configure(bg=bg, fg=INK if selected else MUTED)
            mark.configure(bg=ACCENT if selected else bg)

    def _show_page(self, name: str) -> None:
        self.page = name
        self.home.pack_forget()
        self.settings.pack_forget()
        page = self.home if name == "home" else self.settings
        page.pack(fill="both", expand=True)
        self._paint_nav()

    def _build_home(self) -> None:
        header = tk.Frame(self.home, bg=BG)
        header.pack(fill="x", padx=28, pady=(24, 8))
        tk.Label(header, text="主页", bg=BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
        row = tk.Frame(header, bg=BG)
        row.pack(fill="x", pady=(2, 0))
        tk.Label(row, text="已连接的设备", bg=BG, fg=INK, font=(FONT, 20)).pack(side="left")
        self._text_button(row, "刷新", self.refresh).pack(side="right")
        self.home_status = tk.Label(header, text="正在读取…", bg=BG, fg=MUTED, font=(FONT, 9))
        self.home_status.pack(anchor="w", pady=(6, 0))

        self.canvas = tk.Canvas(self.home, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=28, pady=(8, 20))
        self.cards = tk.Frame(self.canvas, bg=BG)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.cards, anchor="nw")
        self.canvas.bind("<Configure>", self._stretch_cards)
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))
        self._render_cards()

    def _stretch_cards(self, event) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=event.width)

    def _wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _fit_scroll(self) -> None:
        self.cards.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _build_settings(self) -> None:
        canvas = tk.Canvas(self.settings, bg=BG, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        wrap = tk.Frame(canvas, bg=BG)
        window = canvas.create_window((0, 0), window=wrap, anchor="nw")

        def stretch(event) -> None:
            canvas.itemconfigure(window, width=event.width)

        def fit(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def wheel(event) -> None:
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<Configure>", stretch)
        wrap.bind("<Configure>", fit)
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        self.settings_canvas = canvas
        pad = tk.Frame(wrap, bg=BG)
        pad.pack(fill="both", expand=True, padx=28, pady=(24, 20))
        wrap = pad
        tk.Label(wrap, text="设置", bg=BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
        tk.Label(wrap, text="软件参数", bg=BG, fg=INK, font=(FONT, 20)).pack(anchor="w", pady=(2, 4))
        tk.Label(
            wrap, text="保存在这台电脑上，主页和托盘一起用。",
            bg=BG, fg=MUTED, font=(FONT, 9),
        ).pack(anchor="w", pady=(0, 14))

        refresh = self._section(wrap, "刷新间隔", "到点读一次。空闲时不会反复读接收器。")
        self._pills(
            refresh,
            [(str(seconds), label) for seconds, label in INTERVALS],
            lambda: str(self.interval.get()),
            self._pick_interval,
        )

        boot = self._section(wrap, "开机自启", "在启动文件夹放「电量托盘」。任务管理器的「启动」页可以直接禁用。")
        boot_row = tk.Frame(boot, bg=WHITE)
        boot_row.pack(fill="x")
        tk.Label(boot_row, text="登录 Windows 后打开托盘", bg=WHITE, fg=INK, font=(FONT, 10)).pack(side="left")
        self.autostart_switch = Switch(boot_row, self.autostart, self._on_autostart, WHITE)
        self.autostart_switch.pack(side="right")

        power = self._section(
            wrap, "电量预警",
            "没在充电，并且电量降到这个百分比及以下时，Windows 弹出一次提醒。之后每再降 5% 提醒一次。",
        )
        power_row = tk.Frame(power, bg=WHITE)
        power_row.pack(fill="x", pady=(0, 8))
        tk.Label(power_row, text="低电量时弹窗", bg=WHITE, fg=INK, font=(FONT, 10)).pack(side="left")
        Switch(power_row, self.low_battery, self._save_options, WHITE).pack(side="right")
        self._pills(
            power,
            [(str(percent), f"{percent}%") for percent in LOW_PERCENTS],
            lambda: str(self.low_percent.get()),
            self._pick_low_percent,
        )

        dpi = self._section(
            wrap, "DPI 切换提醒",
            "狼蛛 L3、ATK F1、英菲克和多彩醒着的时候会听档位。一切换，主页和弹窗马上更新。快速连切时只保留最新一档。",
        )
        dpi_row = tk.Frame(dpi, bg=WHITE)
        dpi_row.pack(fill="x")
        tk.Label(dpi_row, text="DPI 档位变化时弹窗", bg=WHITE, fg=INK, font=(FONT, 10)).pack(side="left")
        Switch(dpi_row, self.dpi_alert, self._save_options, WHITE).pack(side="right")

        tray = self._section(wrap, "托盘", "图标上的数字是当前已连接设备里最低的电量。")
        buttons = tk.Frame(tray, bg=WHITE)
        buttons.pack(fill="x")
        self._ghost(buttons, "打开托盘", self.open_tray).pack(side="left")
        self._ghost(buttons, "退出托盘", self.quit_tray).pack(side="left", padx=8)
        self._ghost(buttons, "放到桌面", self.make_links).pack(side="left")
        self.status = tk.Label(tray, text="", bg=WHITE, fg=MUTED, font=(FONT, 9), wraplength=560, justify="left")
        self.status.pack(anchor="w", pady=(12, 0))

    def _section(self, parent, title: str, hint: str) -> tk.Frame:
        card = tk.Frame(parent, bg=WHITE, highlightbackground=LINE, highlightthickness=1)
        card.pack(fill="x", pady=(0, 12))
        inner = tk.Frame(card, bg=WHITE)
        inner.pack(fill="x", padx=18, pady=16)
        tk.Label(inner, text=title, bg=WHITE, fg=INK, font=(FONT, 12)).pack(anchor="w")
        tk.Label(
            inner, text=hint, bg=WHITE, fg=MUTED, font=(FONT, 9),
            wraplength=620, justify="left",
        ).pack(anchor="w", pady=(4, 12))
        return inner

    def _pills(self, parent, choices: list[tuple[str, str]], current, on_pick) -> None:
        row = tk.Frame(parent, bg=WHITE)
        row.pack(fill="x")
        buttons: dict[str, tk.Label] = {}

        def paint() -> None:
            selected = current()
            for key, label in buttons.items():
                on = key == selected
                label.configure(bg=ACCENT if on else CHIP, fg=WHITE if on else INK)

        def choose(key: str) -> None:
            on_pick(key)
            paint()

        for key, text in choices:
            label = tk.Label(row, text=text, bg=CHIP, fg=INK, font=(FONT, 9), padx=12, pady=6, cursor="hand2")
            label.pack(side="left", padx=(0, 8), pady=(0, 4))
            label.bind("<Button-1>", lambda _event, item=key: choose(item))
            buttons[key] = label
        paint()

    def _text_button(self, parent, text: str, command):
        button = tk.Label(parent, text=text, bg=ACCENT, fg=WHITE, font=(FONT, 9), padx=14, pady=6, cursor="hand2")
        button.bind("<Button-1>", lambda _event: command())
        return button

    def _ghost(self, parent, text: str, command):
        button = tk.Label(
            parent, text=text, bg=WHITE, fg=INK, font=(FONT, 9),
            padx=12, pady=6, cursor="hand2", highlightbackground=LINE, highlightthickness=1,
        )
        button.bind("<Button-1>", lambda _event: command())
        return button

    def _photo(self, path=None, reading: battery.Reading | None = None) -> tk.PhotoImage | None:
        if reading is not None:
            try:
                blob = product_photo.png_for(reading)
            except OSError:
                blob = None
            if blob:
                key = "photo:" + product_photo.kind_for(reading)
                if key not in self.photos:
                    self.photos[key] = tk.PhotoImage(data=base64.b64encode(blob))
                return self.photos[key]
            path = product_photo.fallback_path(reading)
        if path is None:
            return None
        key = str(path)
        if key in self.photos:
            return self.photos[key]
        try:
            image = tk.PhotoImage(file=key)
        except tk.TclError:
            return None
        self.photos[key] = image
        return image

    def _render_cards(self) -> None:
        if self.loading:
            self._show_empty("正在读取", "只统计已经连上、并且回了电量的鼠标和键盘。")
            return
        if not self.online:
            self._drop_cards()
            self._show_empty("没有连上的设备", "接收器可以一直插着。鼠标或键盘本身醒着并回了电量，才会出现在这里。")
            self.root.after_idle(self._fit_scroll)
            return
        self._hide_empty()
        threshold = int(self.low_percent.get())
        live: list[str] = []
        for reading in self.online:
            self._restore_dpi(reading)
            key = self._dpi_key(reading)
            live.append(key)
            card = self._cards.get(key)
            if card is None:
                self._cards[key] = self._make_card(reading, threshold)
            else:
                self._fill_card(card, reading, threshold)
        for key in list(self._cards):
            if key not in live:
                self._cards.pop(key)["frame"].destroy()
                self._dpi_labels.pop(key, None)
        self.root.after_idle(self._fit_scroll)

    def _drop_cards(self) -> None:
        for card in self._cards.values():
            card["frame"].destroy()
        self._cards.clear()
        self._dpi_labels.clear()

    def _show_empty(self, title: str, body: str) -> None:
        if self._empty is not None:
            self._empty_title.configure(text=title)
            self._empty_body.configure(text=body)
            if not self._empty.winfo_ismapped():
                self._empty.pack(fill="x", pady=(28, 0))
            return
        box = tk.Frame(self.cards, bg=BG)
        box.pack(fill="x", pady=(28, 0))
        photo = self._photo(product_photo.ASSETS / "mouse.png")
        if photo is not None:
            tk.Label(box, image=photo, bg=BG).pack(anchor="w")
        self._empty_title = tk.Label(box, text=title, bg=BG, fg=INK, font=(FONT, 16))
        self._empty_title.pack(anchor="w", pady=(8, 0))
        self._empty_body = tk.Label(box, text=body, bg=BG, fg=MUTED, font=(FONT, 10), wraplength=460, justify="left")
        self._empty_body.pack(anchor="w", pady=(6, 0))
        self._empty = box

    def _hide_empty(self) -> None:
        if self._empty is not None and self._empty.winfo_ismapped():
            self._empty.pack_forget()

    def _meta_base(self, reading: battery.Reading) -> str:
        parts = []
        if reading.source.startswith("有线"):
            parts.append("有线")
        elif reading.source:
            parts.append("2.4G")
        if reading.voltage_mv:
            parts.append(f"{reading.voltage_mv / 1000:.2f} V")
        for part in reading.detail.split("·")[1:]:
            part = part.strip()
            if not part or part.startswith("DPI") or part.endswith("V"):
                continue
            parts.append(part)
        return "  ·  ".join(parts)

    def _make_card(self, reading: battery.Reading, threshold: int) -> dict:
        border = "#e7c4c0" if reading.level is not None and reading.level <= threshold and not reading.charging else LINE
        frame = tk.Frame(self.cards, bg=WHITE, highlightbackground=border, highlightthickness=1)
        frame.pack(fill="x", pady=(0, 12))
        inner = tk.Frame(frame, bg=WHITE)
        inner.pack(fill="x", padx=16, pady=16)
        photo = self._photo(reading=reading)
        slot = tk.Frame(inner, bg=WHITE, width=IMAGE_SLOT, height=photo.height() if photo is not None else 1)
        slot.pack(side="left", fill="y", padx=(0, 20))
        slot.pack_propagate(False)
        if photo is not None:
            tk.Label(slot, image=photo, bg=WHITE).place(relx=0.5, rely=0.5, anchor="center")
        body = tk.Frame(inner, bg=WHITE)
        body.pack(side="left", fill="both", expand=True, anchor="n")
        top = tk.Frame(body, bg=WHITE)
        top.pack(fill="x")
        tk.Label(top, text=reading.name, bg=WHITE, fg=INK, font=(FONT, 14), anchor="w").pack(side="left")
        color = _tone(reading.level, threshold)
        percent = tk.Label(top, text=f"{reading.level}%", bg=WHITE, fg=color, font=(FONT, 26))
        percent.pack(side="right")
        state = "充电中" if reading.charging else "使用电池"
        chip = tk.Label(
            body, text=f"当前状态   {state}",
            bg=SOFT if reading.charging else CHIP, fg=ACCENT if reading.charging else INK,
            font=(FONT, 9), padx=8, pady=3,
        )
        chip.pack(anchor="w", pady=(8, 0))
        base = self._meta_base(reading)
        caption = _dpi_caption(reading)
        meta = tk.Label(
            body, text="  ·  ".join(part for part in (base, caption) if part), bg=WHITE, fg=MUTED,
            font=(FONT, 9), anchor="w", justify="left", wraplength=460,
        )
        meta.pack(anchor="w", pady=(8, 0))
        bar = tk.Canvas(body, height=10, bg=WHITE, highlightthickness=0)
        bar.pack(fill="x", pady=(12, 0))
        bar.level = reading.level
        bar.color = color
        bar.bind("<Configure>", self._paint_bar)
        key = self._dpi_key(reading)
        self._dpi_labels[key] = (meta, base)

        def align(slot=slot, body=body, photo=photo) -> None:
            body.update_idletasks()
            height = body.winfo_reqheight()
            if photo is not None:
                height = max(height, photo.height())
            slot.configure(height=max(1, height))

        body.bind("<Configure>", lambda _event: align(), add="+")
        inner.after_idle(align)
        return {
            "frame": frame, "percent": percent, "chip": chip, "meta": meta, "bar": bar,
            "base": base, "level": reading.level, "color": color, "charging": reading.charging,
        }

    def _fill_card(self, card: dict, reading: battery.Reading, threshold: int) -> None:
        border = "#e7c4c0" if reading.level is not None and reading.level <= threshold and not reading.charging else LINE
        if str(card["frame"].cget("highlightbackground")) != border:
            card["frame"].configure(highlightbackground=border)
        color = _tone(reading.level, threshold)
        percent = f"{reading.level}%"
        if card["percent"].cget("text") != percent or card["color"] != color:
            card["percent"].configure(text=percent, fg=color)
            card["color"] = color
            card["level"] = reading.level
            card["bar"].level = reading.level
            card["bar"].color = color
            self._redraw_bar(card["bar"])
        charging = bool(reading.charging)
        if card["charging"] != charging:
            state = "充电中" if charging else "使用电池"
            card["chip"].configure(
                text=f"当前状态   {state}",
                bg=SOFT if charging else CHIP, fg=ACCENT if charging else INK,
            )
            card["charging"] = charging
        base = self._meta_base(reading)
        caption = _dpi_caption(reading)
        text = "  ·  ".join(part for part in (base, caption) if part)
        if card["meta"].cget("text") != text:
            card["meta"].configure(text=text)
        card["base"] = base
        self._dpi_labels[self._dpi_key(reading)] = (card["meta"], base)

    def _dpi_key(self, reading: battery.Reading) -> str:
        return f"{reading.vid:04X}:{reading.pid:04X}"

    def _restore_dpi(self, reading: battery.Reading) -> None:
        """A battery refresh does not carry DPI. Keep the last gear on the card."""
        key = self._dpi_key(reading)
        if reading.dpi:
            self._dpi_known[key] = (reading.dpi, reading.dpi_value)
            return
        known = self._dpi_known.get(key)
        if known:
            reading.dpi, reading.dpi_value = known

    def _paint_bar(self, event) -> None:
        self._redraw_bar(event.widget)

    def _redraw_bar(self, canvas: tk.Canvas) -> None:
        level = getattr(canvas, "level", None)
        fill = getattr(canvas, "color", OK)
        canvas.delete("all")
        width = max(canvas.winfo_width(), 1)
        canvas.create_rectangle(0, 2, width, 8, fill="#eeeae2", outline="")
        if level is not None and level > 0:
            canvas.create_rectangle(0, 2, max(4, width * level / 100), 8, fill=fill, outline="")

    def _pick_interval(self, value: str) -> None:
        self.interval.set(int(value))
        self._save_options()
        self._arm()
        label = dict(INTERVALS).get(self.interval.get(), value)
        self.status.configure(text=f"刷新间隔已改为 {label}。")

    def _pick_low_percent(self, value: str) -> None:
        self.low_percent.set(int(value))
        self._save_options()
        self._render_cards()
        self.status.configure(text=f"低电量线已改为 {value}%。")

    def _save_options(self) -> None:
        setup.save_config({
            "interval": int(self.interval.get()),
            "low_battery": bool(self.low_battery.get()),
            "low_percent": int(self.low_percent.get()),
            "dpi_alert": bool(self.dpi_alert.get()),
        })
        dpi_watch.configure(bool(self.dpi_alert.get()))
        setup.post_to_tray(setup.MSG_RELOAD)

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
            self.autostart_switch.redraw()
            self.syncing = False
            self.status.configure(text=f"没能改开机自启：{exc}")

    def _sync_autostart(self) -> None:
        if not self.alive:
            return
        actual = setup.is_autostart_enabled()
        if actual != bool(self.autostart.get()):
            self.syncing = True
            self.autostart.set(actual)
            self.autostart_switch.redraw()
            self.syncing = False
        self.root.after(2000, self._sync_autostart)

    def _sync_live_dpi(self) -> None:
        if not self.alive:
            return
        self.root.after(40, self._sync_live_dpi)
        try:
            mtime = alerts.LIVE_PATH.stat().st_mtime_ns
        except OSError:
            return
        if mtime == self._dpi_mtime:
            return
        self._dpi_mtime = mtime
        try:
            found = json.loads(alerts.LIVE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(found, dict):
            return
        changed = False
        for reading in self.online:
            info = found.get(f"{reading.vid:04X}:{reading.pid:04X}")
            if not isinstance(info, dict):
                continue
            gear = info.get("gear")
            value = info.get("value")
            if not isinstance(gear, int) or not gear:
                continue
            dpi_value = value if isinstance(value, int) else None
            previous = self._dpi_known.get(self._dpi_key(reading))
            if dpi_value is None and previous is not None:
                dpi_value = previous[1]
            self._dpi_known[self._dpi_key(reading)] = (gear, dpi_value)
            if gear != reading.dpi or dpi_value != reading.dpi_value:
                reading.dpi = gear
                reading.dpi_value = dpi_value
                pair = self._dpi_labels.get(self._dpi_key(reading))
                if pair is not None:
                    label, base = pair
                    caption = _dpi_caption(reading)
                    label.configure(text="  ·  ".join(part for part in (base, caption) if part))
                else:
                    changed = True
        if changed:
            place = self.canvas.yview()[0]
            self._render_cards()
            self.canvas.yview_moveto(place)

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
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        try:
            readings = devices.read_devices()
        except Exception as exc:
            self.queue.put(("err", str(exc)))
            return
        self.queue.put(("ok", readings))

    def _pump(self) -> None:
        if not self.alive:
            return
        try:
            while True:
                self._show(*self.queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _show(self, kind: str, payload) -> None:
        self.busy = False
        self.loading = False
        if kind == "err":
            self.home_status.configure(text=f"读取失败：{payload}")
            self._render_cards()
            return
        readings = list(payload)
        self.online = devices.connected(readings)
        hidden = len(readings) - len(self.online)
        try:
            alerts.notify(self.online)
        except OSError:
            pass
        stamp = time.strftime("%H:%M:%S")
        if not self.online and hidden:
            self.home_status.configure(text=f"{stamp}  ·  {hidden} 个接收器还在，外设没有连上")
        elif hidden:
            self.home_status.configure(text=f"{stamp} 更新  ·  已隐藏 {hidden} 个没连上的接收器")
        elif self.online:
            self.home_status.configure(text=f"{stamp} 更新  ·  {len(self.online)} 台设备")
        else:
            self.home_status.configure(text=f"{stamp}  ·  没有已连接的设备")
        self._render_cards()

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
        dpi_watch.stop()
        if self._timer is not None:
            self.root.after_cancel(self._timer)
        try:
            self.canvas.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass
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
