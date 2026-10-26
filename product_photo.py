"""Load the product photos for the home-page cards.

On this machine the originals sit in the driver folders next to the repo.
Windows Imaging Component decodes those WebP files, knocks out the black
studio background, and returns a small PNG for the white card.

assets/photos keeps a copy of that result. Another computer that only has
the git repo still shows the real product, not the generic icon.
"""

from __future__ import annotations

import struct
import zlib
from ctypes import wintypes
from pathlib import Path

import ctypes

from delux_battery import Reading

ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
ASSETS = ROOT / "assets"
PHOTOS = ASSETS / "photos"

# (folder name piece, filename prefix, fit box)
# F1, V75X and IN10R fill their frames, so a shared box makes them larger
# than L3. Those three are scaled again to L3's visible product size.
_SOURCES = {
    "f1": ("ATK系列驱动", "lev-", (210, 280)),
    "v75x": ("ATK系列驱动", "v75x-", (520, 200)),
    "l3": ("狼蛛L3", "silver-", (210, 280)),
    "m900": ("多彩M900", "5d01", (210, 280)),
    "inphic": ("英菲克", "IN10R", (210, 280)),
}
_MATCH_L3 = {"f1", "v75x", "inphic"}

_cache: dict[tuple, bytes] = {}

ole32 = ctypes.WinDLL("ole32", use_last_error=True)
ole32.CoInitialize.argtypes = [ctypes.c_void_p]
ole32.CoCreateInstance.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoCreateInstance.restype = ctypes.c_long
_com_ready = False


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> GUID:
    a, b, c, rest = text.split("-", 3)
    d, e = rest.split("-", 1)
    item = GUID()
    item.Data1 = int(a, 16)
    item.Data2 = int(b, 16)
    item.Data3 = int(c, 16)
    item.Data4[:] = bytes.fromhex(d + e)
    return item


_FACTORY = _guid("cacaf262-9370-4615-a13b-9f5539da4c0a")
_FACTORY_IID = _guid("ec5ec8a9-c395-4314-9c77-54d7a935ff70")
_PBGRA = _guid("6fddc324-4e03-4bfe-b185-3d77768dc910")


def _com(unk, index, restype, argtypes, *args):
    table = ctypes.cast(ctypes.cast(unk, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    func = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table[index])
    return func(unk, *args)


def _release(unk) -> None:
    if unk:
        _com(unk, 2, ctypes.c_ulong, [])


def _check(hr, step: str) -> None:
    if hr < 0:
        raise OSError(f"{step} {hr & 0xFFFFFFFF:08x}")


def _factory():
    global _com_ready
    if not _com_ready:
        ole32.CoInitialize(None)
        _com_ready = True
    factory = ctypes.c_void_p()
    _check(ole32.CoCreateInstance(
        ctypes.byref(_FACTORY), None, 1, ctypes.byref(_FACTORY_IID), ctypes.byref(factory),
    ), "WIC")
    return factory


def _decode(path: Path, max_w: int, max_h: int) -> tuple[int, int, bytes]:
    factory = _factory()
    decoder = ctypes.c_void_p()
    frame = ctypes.c_void_p()
    scaler = ctypes.c_void_p()
    converter = ctypes.c_void_p()
    try:
        _check(_com(
            factory, 3, ctypes.c_long,
            [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)],
            str(path), None, 0x80000000, 0, ctypes.byref(decoder),
        ), "打开图片")
        _check(_com(
            decoder, 13, ctypes.c_long,
            [wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)],
            0, ctypes.byref(frame),
        ), "读取画面")
        width = wintypes.UINT()
        height = wintypes.UINT()
        _check(_com(
            frame, 3, ctypes.c_long,
            [ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)],
            ctypes.byref(width), ctypes.byref(height),
        ), "图片尺寸")
        scale = min(max_w / width.value, max_h / height.value, 1)
        out_w = max(1, int(width.value * scale))
        out_h = max(1, int(height.value * scale))
        _check(_com(
            factory, 11, ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(scaler),
        ), "缩放")
        _check(_com(
            scaler, 8, ctypes.c_long,
            [ctypes.c_void_p, wintypes.UINT, wintypes.UINT, wintypes.UINT],
            frame, out_w, out_h, 3,
        ), "缩放初始化")
        _check(_com(
            factory, 10, ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(converter),
        ), "转像素")
        _check(_com(
            converter, 8, ctypes.c_long,
            [ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.UINT, ctypes.c_void_p, ctypes.c_double, wintypes.UINT],
            scaler, ctypes.byref(_PBGRA), 0, None, 0.0, 0,
        ), "转像素初始化")
        stride = out_w * 4
        buf = ctypes.create_string_buffer(stride * out_h)
        _check(_com(
            converter, 7, ctypes.c_long,
            [ctypes.c_void_p, wintypes.UINT, wintypes.UINT, ctypes.c_void_p],
            None, stride, stride * out_h, ctypes.cast(buf, ctypes.c_void_p),
        ), "复制像素")
        return out_w, out_h, buf.raw
    finally:
        _release(converter)
        _release(scaler)
        _release(frame)
        _release(decoder)
        _release(factory)


def _knock_out_black(pixels: bytearray, width: int, height: int) -> None:
    def background(index: int) -> bool:
        blue, green, red, alpha = pixels[index:index + 4]
        if alpha < 20:
            return True
        return red < 16 and green < 16 and blue < 16

    corners = (0, (width - 1) * 4, (height - 1) * width * 4, ((height - 1) * width + width - 1) * 4)
    if not all(background(index) for index in corners):
        return
    seen = bytearray(width * height)
    stack = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
    while stack:
        x, y = stack.pop()
        if x < 0 or y < 0 or x >= width or y >= height:
            continue
        pos = y * width + x
        if seen[pos]:
            continue
        index = pos * 4
        if not background(index):
            continue
        seen[pos] = 1
        pixels[index + 3] = 0
        stack.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))


def _content_span(pixels: bytearray, width: int, height: int) -> int:
    min_x, min_y, max_x, max_y = width, height, -1, -1
    for y in range(height):
        for x in range(width):
            index = (y * width + x) * 4
            blue, green, red, alpha = pixels[index:index + 4]
            if alpha < 20 or (red > 248 and green > 248 and blue > 248):
                continue
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)
    if max_x < 0:
        return max(width, height)
    return max(max_x - min_x + 1, max_y - min_y + 1)


def _scale_to(pixels: bytes, width: int, height: int, target_long: int) -> tuple[int, int, bytearray]:
    current = _content_span(bytearray(pixels), width, height)
    if current <= target_long:
        return width, height, bytearray(pixels)
    scale = target_long / current
    out_w = max(1, int(round(width * scale)))
    out_h = max(1, int(round(height * scale)))
    out = bytearray(out_w * out_h * 4)
    for y in range(out_h):
        y0 = y * height // out_h
        y1 = max(y0 + 1, (y + 1) * height // out_h)
        for x in range(out_w):
            x0 = x * width // out_w
            x1 = max(x0 + 1, (x + 1) * width // out_w)
            total = [0, 0, 0, 0]
            count = 0
            for yy in range(y0, min(y1, height)):
                row = yy * width
                for xx in range(x0, min(x1, width)):
                    index = (row + xx) * 4
                    total[0] += pixels[index]
                    total[1] += pixels[index + 1]
                    total[2] += pixels[index + 2]
                    total[3] += pixels[index + 3]
                    count += 1
            dest = (y * out_w + x) * 4
            for channel in range(4):
                out[dest + channel] = total[channel] // count
    return out_w, out_h, out


_l3_span_value: int | None = None


def _l3_span() -> int:
    global _l3_span_value
    if _l3_span_value is not None:
        return _l3_span_value
    path = source_for("l3")
    if path is None:
        _l3_span_value = 181
        return _l3_span_value
    _name, _prefix, box = _SOURCES["l3"]
    width, height, raw = _decode(path, box[0], box[1])
    pixels = bytearray(raw)
    _knock_out_black(pixels, width, height)
    _l3_span_value = _content_span(pixels, width, height)
    return _l3_span_value


def _png(width: int, height: int, bgra: bytes) -> bytes:
    pixels = bytearray(bgra)
    _knock_out_black(pixels, width, height)
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            index = (y * width + x) * 4
            blue, green, red, alpha = pixels[index:index + 4]
            mix = alpha / 255
            row.append(int(red * mix + 255 * (1 - mix)))
            row.append(int(green * mix + 255 * (1 - mix)))
            row.append(int(blue * mix + 255 * (1 - mix)))
        rows.append(b"\x00" + bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b"")


def _folder(piece: str) -> Path | None:
    direct = PARENT / piece
    if direct.is_dir():
        return direct
    for child in PARENT.iterdir():
        if child.is_dir() and piece in child.name:
            return child
    return None


def source_for(kind: str) -> Path | None:
    piece, prefix, _box = _SOURCES[kind]
    folder = _folder(piece)
    if folder is None:
        return None
    found = [path for path in folder.iterdir() if path.is_file() and path.name.lower().startswith(prefix.lower())]
    photos = [path for path in found if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}]
    return photos[0] if photos else None


def kind_for(reading: Reading) -> str:
    name = reading.name.upper()
    if "V75" in name or reading.vid == 0x320F:
        return "v75x"
    if "F1" in name or reading.vid == 0x373B:
        return "f1"
    if "狼蛛" in reading.name or reading.vid in (0xA8A4, 0xA8A5):
        return "l3"
    if "多彩" in reading.name or "M900" in name or reading.vid == 0x3554:
        return "m900"
    if "英菲克" in reading.name or "INPHIC" in name or reading.vid == 0x1D57:
        return "inphic"
    if "键盘" in reading.name:
        return "keyboard"
    return "mouse"


def _bundled_png(kind: str) -> bytes | None:
    path = PHOTOS / f"{kind}.png"
    if not path.is_file():
        return None
    key = ("bundled", str(path), path.stat().st_mtime_ns)
    cached = _cache.get(key)
    if cached is None:
        cached = path.read_bytes()
        _cache[key] = cached
    return cached


def png_for(reading: Reading) -> bytes | None:
    kind = kind_for(reading)
    if kind not in _SOURCES:
        return None
    path = source_for(kind)
    if path is None:
        return _bundled_png(kind)
    key = (str(path), path.stat().st_mtime_ns, _SOURCES[kind][2], kind in _MATCH_L3)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    _folder_name, _prefix, box = _SOURCES[kind]
    width, height, raw = _decode(path, box[0], box[1])
    pixels = bytearray(raw)
    _knock_out_black(pixels, width, height)
    if kind in _MATCH_L3:
        width, height, pixels = _scale_to(pixels, width, height, _l3_span())
    encoded = _png(width, height, pixels)
    _cache[key] = encoded
    return encoded


def fallback_path(reading: Reading) -> Path:
    kind = kind_for(reading)
    path = ASSETS / f"{kind}.png"
    if path.exists():
        return path
    generic = "keyboard.png" if kind in {"v75x", "keyboard"} else "mouse.png"
    return ASSETS / generic
