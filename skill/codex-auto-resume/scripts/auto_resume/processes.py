import ctypes
import os
import signal
import subprocess
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


def _proc_fields(pid):
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return None
    try:
        return stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None


def _ps_identity(pid):
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)], text=True, encoding="utf-8",
            errors="replace", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3, shell=False, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip() if result.returncode == 0 else ""
    return f"ps:{value}" if value else None


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
    fields = _proc_fields(pid)
    # Field 22 is starttime; split after the final ')' to tolerate spaces in comm.
    return f"proc:{fields[19]}" if fields and len(fields) > 19 else _ps_identity(pid)


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
            fields = _proc_fields(pid)
            running = not fields or fields[0] != "Z"
        except OSError:
            running = False
    if not running:
        return False
    return expected_identity is None or process_identity(pid) == expected_identity


def terminate_process_tree(proc, term_timeout=0.5, kill_timeout=2):
    """Best-effort termination of a process and every descendant it launched."""
    pid = getattr(proc, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, shell=False,
                creationflags=flags, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(pid, getattr(signal, "SIGTERM", 15))
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=term_timeout)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    if os.name != "nt":
        try:
            os.killpg(pid, getattr(signal, "SIGKILL", 9))
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=kill_timeout)
    except (OSError, subprocess.TimeoutExpired):
        pass
