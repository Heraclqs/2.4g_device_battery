"""32x32 tray icons drawn with GDI. No Pillow."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

BI_RGB = 0
DIB_RGB_COLORS = 0
FW_BOLD = 700
CLEARTYPE_QUALITY = 5
DT_CENTER = 0x01
DT_VCENTER = 0x04
DT_SINGLELINE = 0x20
TRANSPARENT = 1


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.DestroyIcon.argtypes = [wintypes.HICON]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.CreateBitmap.restype = wintypes.HBITMAP
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.CreateFontW.restype = wintypes.HFONT
gdi32.CreateFontW.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
]
gdi32.CreateBitmap.restype = wintypes.HBITMAP
user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(RECT), wintypes.UINT]
user32.CreateIconIndirect.restype = wintypes.HICON
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), wintypes.HBRUSH]


def _text_color(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    # Dark digits on the bright green/yellow badges, white on red and grey.
    if 0.3 * r + 0.59 * g + 0.11 * b > 160:
        return 0x00141414
    return 0x00FFFFFF


def make_hicon(text: str, rgb: tuple[int, int, int], size: int = 32) -> int:
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = size
    info.bmiHeader.biHeight = -size
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB
    bits = ctypes.c_void_p()
    color = gdi32.CreateDIBSection(dc, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(dc, color)
    colorref = rgb[0] | (rgb[1] << 8) | (rgb[2] << 16)
    brush = gdi32.CreateSolidBrush(colorref)
    rect = RECT(0, 0, size, size)
    user32.FillRect(dc, ctypes.byref(rect), brush)
    height = -22 if len(text) <= 2 else -15
    font = gdi32.CreateFontW(height, 0, 0, 0, FW_BOLD, 0, 0, 0, 1, 0, 0, CLEARTYPE_QUALITY, 0, "Segoe UI")
    old_font = gdi32.SelectObject(dc, font)
    gdi32.SetBkMode(dc, TRANSPARENT)
    gdi32.SetTextColor(dc, _text_color(rgb))
    user32.DrawTextW(dc, text, -1, ctypes.byref(rect), DT_CENTER | DT_VCENTER | DT_SINGLELINE)
    # GDI leaves alpha at 0, which makes a 32-bit icon invisible.
    pixel = ctypes.cast(bits, ctypes.POINTER(ctypes.c_uint32))
    for index in range(size * size):
        pixel[index] = (pixel[index] & 0x00FFFFFF) | 0xFF000000
    gdi32.SelectObject(dc, old_font)
    gdi32.SelectObject(dc, old)
    mask_bytes = (ctypes.c_ubyte * ((size * size) // 8))()
    mask = gdi32.CreateBitmap(size, size, 1, 1, mask_bytes)
    icon = ICONINFO(True, 0, 0, mask, color)
    hicon = user32.CreateIconIndirect(ctypes.byref(icon))
    gdi32.DeleteObject(font)
    gdi32.DeleteObject(brush)
    gdi32.DeleteObject(mask)
    gdi32.DeleteObject(color)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(None, screen)
    return hicon


def destroy_icon(hicon: int) -> None:
    if hicon:
        user32.DestroyIcon(hicon)
