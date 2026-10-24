"""Windows HID helpers. Standard library and ctypes only.

Reads and writes use two handles. A blocking read is never left pending on the
same handle that a write goes out on — that combination can kill the process.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
INVALID_HANDLE = wintypes.HANDLE(-1).value


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", ctypes.c_ulong),
        ("VendorID", ctypes.c_ushort),
        ("ProductID", ctypes.c_ushort),
        ("VersionNumber", ctypes.c_ushort),
    ]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED),
]
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
]
kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetProductString.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_ulong]
hid.HidD_GetManufacturerString.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_ulong]
hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid.HidP_GetCaps.restype = ctypes.c_long


@dataclass
class HidDevice:
    path: str
    vid: int
    pid: int
    version: int
    usage_page: int
    usage: int
    input_len: int
    output_len: int
    feature_len: int
    manufacturer: str
    product: str


def _hid_guid() -> GUID:
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    return guid


def _device_paths() -> list[str]:
    guid = _hid_guid()
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    handle = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)
    if not handle or handle == INVALID_HANDLE:
        return []
    paths: list[str] = []
    index = 0
    try:
        while True:
            data = SP_DEVICE_INTERFACE_DATA()
            data.cbSize = ctypes.sizeof(data)
            if not setupapi.SetupDiEnumDeviceInterfaces(handle, None, ctypes.byref(guid), index, ctypes.byref(data)):
                break
            needed = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(handle, ctypes.byref(data), None, 0, ctypes.byref(needed), None)
            buf = ctypes.create_string_buffer(needed.value)
            ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = 8
            if setupapi.SetupDiGetDeviceInterfaceDetailW(
                handle, ctypes.byref(data), buf, needed, ctypes.byref(needed), None
            ):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(handle)
    return paths


def _open(path: str, access: int, overlapped: bool = False):
    flags = FILE_FLAG_OVERLAPPED if overlapped else 0
    handle = kernel32.CreateFileW(
        path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, flags, None
    )
    if handle == INVALID_HANDLE:
        return None
    return handle


def _wide(handle, getter) -> str:
    buf = ctypes.create_string_buffer(512)
    if not getter(handle, buf, 512):
        return ""
    return ctypes.wstring_at(buf)


def enumerate_hid() -> list[HidDevice]:
    found: list[HidDevice] = []
    for path in _device_paths():
        handle = _open(path, GENERIC_READ | GENERIC_WRITE) or _open(path, GENERIC_READ) or _open(path, 0)
        if handle is None:
            continue
        try:
            attr = HIDD_ATTRIBUTES()
            attr.Size = ctypes.sizeof(attr)
            if not hid.HidD_GetAttributes(handle, ctypes.byref(attr)):
                continue
            preparsed = ctypes.c_void_p()
            if not hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
                continue
            caps = HIDP_CAPS()
            try:
                hid.HidP_GetCaps(preparsed, ctypes.byref(caps))
            finally:
                hid.HidD_FreePreparsedData(preparsed)
            found.append(HidDevice(
                path=path,
                vid=attr.VendorID,
                pid=attr.ProductID,
                version=attr.VersionNumber,
                usage_page=caps.UsagePage,
                usage=caps.Usage,
                input_len=caps.InputReportByteLength,
                output_len=caps.OutputReportByteLength,
                feature_len=caps.FeatureReportByteLength,
                manufacturer=_wide(handle, hid.HidD_GetManufacturerString),
                product=_wide(handle, hid.HidD_GetProductString),
            ))
        finally:
            kernel32.CloseHandle(handle)
    return found


class HidIo:
    """One overlapped reader and one synchronous writer."""

    def __init__(self, path: str, in_len: int):
        self.in_len = in_len
        self.event = None
        self.ov = OVERLAPPED()
        self.reader = _open(path, GENERIC_READ, overlapped=True)
        self.writer = _open(path, GENERIC_WRITE)
        if self.reader is None or self.writer is None:
            self.close()
            err = ctypes.get_last_error()
            raise OSError(err, "打不开 HID 配置口")
        self.event = kernel32.CreateEventW(None, True, False, None)
        self.ov = OVERLAPPED()
        self.ov.hEvent = self.event
        self.buf = ctypes.create_string_buffer(in_len)
        self.got = wintypes.DWORD()
        self._sync: bytes | None = None
        self._arm()

    def _arm(self) -> None:
        self._sync = None
        kernel32.ResetEvent(self.event)
        self.got = wintypes.DWORD()
        ok = kernel32.ReadFile(self.reader, self.buf, self.in_len, ctypes.byref(self.got), ctypes.byref(self.ov))
        if ok:
            self._sync = bytes(self.buf.raw[: self.got.value])
            return
        err = ctypes.get_last_error()
        if err != ERROR_IO_PENDING:
            raise OSError(err, "ReadFile")

    def write(self, report: bytes) -> None:
        payload = ctypes.create_string_buffer(report, len(report))
        written = wintypes.DWORD()
        if not kernel32.WriteFile(self.writer, payload, len(report), ctypes.byref(written), None):
            raise OSError(ctypes.get_last_error(), "WriteFile")
        if written.value != len(report):
            raise OSError(0, f"只写了 {written.value} 字节")

    def read(self, timeout_ms: int) -> bytes | None:
        if self._sync is not None:
            data = self._sync
            self._arm()
            return data
        if kernel32.WaitForSingleObject(self.event, timeout_ms) != WAIT_OBJECT_0:
            return None
        if not kernel32.GetOverlappedResult(self.reader, ctypes.byref(self.ov), ctypes.byref(self.got), False):
            raise OSError(ctypes.get_last_error(), "GetOverlappedResult")
        data = bytes(self.buf.raw[: self.got.value])
        self._arm()
        return data

    def close(self) -> None:
        if getattr(self, "reader", None):
            kernel32.CancelIoEx(self.reader, ctypes.byref(self.ov))
            if getattr(self, "event", None):
                kernel32.WaitForSingleObject(self.event, 200)
        for handle in (getattr(self, "event", None), getattr(self, "reader", None), getattr(self, "writer", None)):
            if handle:
                kernel32.CloseHandle(handle)
        self.event = None
        self.reader = None
        self.writer = None

    def __enter__(self) -> "HidIo":
        return self

    def __exit__(self, *_) -> None:
        self.close()
