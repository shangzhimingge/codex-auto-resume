import ctypes
import os
from pathlib import Path

if os.name == "nt":
    from ctypes import wintypes


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
    )
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL


def _windows_handle(pid):
    return _kernel32.OpenProcess(0x1000, False, pid)


def process_identity(pid):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        handle = _windows_handle(pid)
        if not handle:
            return None
        try:
            creation, exit_time, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
            if not _kernel32.GetProcessTimes(
                    handle, ctypes.byref(creation), ctypes.byref(exit_time),
                    ctypes.byref(kernel), ctypes.byref(user)):
                return None
            return f"win:{creation.high:08x}{creation.low:08x}"
        finally:
            _kernel32.CloseHandle(handle)
    stat = Path(f"/proc/{pid}/stat")
    try:
        # Field 22 is starttime; split after the final ')' to tolerate spaces in comm.
        fields = stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return f"proc:{fields[19]}"
    except (OSError, IndexError):
        return None


def process_is_running(pid, expected_identity=None):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        handle = _windows_handle(pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            running = bool(_kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            _kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            running = fields[0] != "Z"
        except OSError:
            running = False
        except IndexError:
            running = False
    if not running:
        return False
    return expected_identity is None or process_identity(pid) == expected_identity
