import ctypes
import os
import signal
import subprocess
import time
from pathlib import Path

if os.name == "nt":
    from ctypes import wintypes


class ProcessCleanupError(RuntimeError):
    pass


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


_WINDOWS_APIS_AVAILABLE = os.name == "nt"
if _WINDOWS_APIS_AVAILABLE:
    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]


    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]


    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]


    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


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
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W))
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W))
    _kernel32.Process32NextW.restype = wintypes.BOOL


def _windows_error(action):
    return OSError(ctypes.get_last_error(), action)


def _windows_handle(pid, access=0x1000):
    return _kernel32.OpenProcess(access, False, pid)


def _identity_from_windows_handle(handle):
    creation, exit_time, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
    if not _kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_time),
            ctypes.byref(kernel), ctypes.byref(user)):
        return None
    return f"win:{creation.high:08x}{creation.low:08x}"


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
            return _identity_from_windows_handle(handle)
        finally:
            _kernel32.CloseHandle(handle)
    fields = _proc_fields(pid)
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
            identity = (_identity_from_windows_handle(handle)
                        if running and expected_identity is not None else None)
        finally:
            _kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            fields = _proc_fields(pid)
            running = not fields or fields[0] != "Z"
            identity = process_identity(pid) if running and expected_identity is not None else None
        except OSError:
            running, identity = False, None
    return running and (expected_identity is None or identity == expected_identity)


def _process_parents():
    if not _WINDOWS_APIS_AVAILABLE:
        return {}
    snapshot = _kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snapshot or int(snapshot) == invalid:
        return {}
    parents = {}
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        present = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            present = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return parents


def _terminate_process(pid, expected_identity):
    if not _WINDOWS_APIS_AVAILABLE:
        return False
    handle = _windows_handle(pid, 0x0001 | 0x1000)
    if not handle:
        return False
    try:
        if _identity_from_windows_handle(handle) != expected_identity:
            return False
        return bool(_kernel32.TerminateProcess(handle, 1))
    finally:
        _kernel32.CloseHandle(handle)


class ProcessTreeGuard:
    """Owns and deterministically drains a spawned subprocess tree."""

    def __init__(self, platform_name=None, drain_timeout=3.0, poll_interval=0.02):
        self.platform_name = platform_name or os.name
        self.drain_timeout = max(0.0, float(drain_timeout))
        self.poll_interval = max(0.0, float(poll_interval))
        self._proc = None
        self._root_pid = None
        self._tracked = {}
        self._job_handle = None
        self.assignment_error = None
        self.descendant_assignment_errors = []
        self.creation_error = None
        self._closed = False
        if self.platform_name == "nt" and _WINDOWS_APIS_AVAILABLE:
            try:
                self._job_handle = self._create_job()
            except OSError as exc:
                self.creation_error = exc

    def _create_job(self):
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise _windows_error("CreateJobObjectW failed")
        info = _JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not _kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = _windows_error("SetInformationJobObject failed")
            _kernel32.CloseHandle(handle)
            raise error
        return handle

    def attach(self, proc):
        self._proc = proc
        self._root_pid = getattr(proc, "pid", None)
        identity = process_identity(self._root_pid)
        if identity is not None:
            self._tracked[self._root_pid] = identity
        if self.platform_name == "nt" and self._job_handle:
            try:
                self._assign_job(proc)
            except OSError as exc:
                self.assignment_error = exc
        if self.platform_name == "nt":
            self._snapshot_descendants()
        return proc

    def _assign_job(self, proc):
        process_handle = getattr(proc, "_handle", None)
        if not isinstance(process_handle, int) or isinstance(process_handle, bool):
            process_handle = None
        opened = False
        if not process_handle:
            process_handle = _windows_handle(self._root_pid, 0x0001 | 0x0100 | 0x1000)
            opened = bool(process_handle)
        if not process_handle:
            raise _windows_error("OpenProcess for job assignment failed")
        try:
            if not _kernel32.AssignProcessToJobObject(self._job_handle, process_handle):
                raise _windows_error("AssignProcessToJobObject failed")
        finally:
            if opened:
                _kernel32.CloseHandle(process_handle)

    def _assign_pid_to_job(self, pid):
        process_handle = _windows_handle(pid, 0x0001 | 0x0100 | 0x1000)
        if not process_handle:
            raise _windows_error("OpenProcess for descendant assignment failed")
        try:
            if not _kernel32.AssignProcessToJobObject(self._job_handle, process_handle):
                raise _windows_error("AssignProcessToJobObject descendant failed")
        finally:
            _kernel32.CloseHandle(process_handle)

    def _snapshot_descendants(self):
        if not self._tracked:
            return
        parents = _process_parents()
        live_parents = {
            pid for pid, identity in self._tracked.items()
            if process_is_running(pid, identity)
        }
        changed = True
        while changed:
            changed = False
            for pid, parent in parents.items():
                if pid in self._tracked or parent not in live_parents:
                    continue
                identity = process_identity(pid)
                if identity is not None:
                    self._tracked[pid] = identity
                    live_parents.add(pid)
                    changed = True
                    if self._job_handle:
                        try:
                            self._assign_pid_to_job(pid)
                        except OSError as exc:
                            self.descendant_assignment_errors.append((pid, exc))

    def _terminate_job(self):
        if self._job_handle and not _kernel32.TerminateJobObject(self._job_handle, 1):
            raise _windows_error("TerminateJobObject failed")

    def _taskkill_tree(self):
        ordered = ([self._root_pid] if self._root_pid in self._tracked else [])
        ordered.extend(pid for pid in self._tracked if pid != self._root_pid)
        for pid in ordered:
            expected = self._tracked.get(pid)
            if expected is not None and not process_is_running(pid, expected):
                continue
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5, check=False,
            )

    def _terminate_tracked(self):
        for pid, identity in tuple(self._tracked.items()):
            if pid != self._root_pid and process_identity(pid) == identity:
                _terminate_process(pid, identity)

    def _job_empty(self):
        if not self._job_handle:
            return True
        return _kernel32.WaitForSingleObject(self._job_handle, 0) == 0

    def _drain_tracked(self):
        deadline = time.monotonic() + self.drain_timeout
        while True:
            self._snapshot_descendants()
            survivors = [
                pid for pid, identity in self._tracked.items()
                if process_is_running(pid, identity)
            ]
            job_active = not self._job_empty()
            if not survivors and not job_active:
                return
            if time.monotonic() >= deadline:
                detail = ",".join(map(str, survivors)) or "job"
                raise ProcessCleanupError(f"process tree did not drain: {detail}")
            if self.poll_interval:
                time.sleep(self.poll_interval)

    def _close_job(self):
        if self._job_handle:
            handle, self._job_handle = self._job_handle, None
            if not _kernel32.CloseHandle(handle):
                raise _windows_error("CloseHandle(job) failed")

    def close(self, term_timeout=0.5, kill_timeout=2):
        if self._closed:
            return
        self._closed = True
        if self._proc is None:
            try:
                self._close_job()
            except OSError as exc:
                raise ProcessCleanupError(str(exc)) from exc
            return
        if self.platform_name != "nt":
            if term_timeout == 0.5 and kill_timeout == 2:
                terminate_process_tree(self._proc)
            else:
                terminate_process_tree(self._proc, term_timeout, kill_timeout)
            return
        cleanup_error = None
        try:
            self._snapshot_descendants()
            try:
                self._terminate_job()
            except OSError:
                pass
            try:
                self._taskkill_tree()
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                self._proc.kill()
            except OSError:
                pass
            self._terminate_tracked()
            try:
                self._proc.wait(timeout=term_timeout)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._proc.wait(timeout=kill_timeout)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._drain_tracked()
        except BaseException as exc:
            cleanup_error = exc
        try:
            self._close_job()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if cleanup_error is not None:
            if isinstance(cleanup_error, ProcessCleanupError):
                raise cleanup_error
            raise ProcessCleanupError(str(cleanup_error)) from cleanup_error


def terminate_process_tree(proc, term_timeout=0.5, kill_timeout=2):
    """Best-effort termination of a process and every descendant it launched."""
    pid = getattr(proc, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return
    if os.name == "nt":
        guard = ProcessTreeGuard(platform_name="nt",
                                 drain_timeout=max(term_timeout + kill_timeout, 0.1))
        guard.attach(proc)
        try:
            guard.close(term_timeout, kill_timeout)
        except ProcessCleanupError:
            pass
        return
    try:
        os.killpg(pid, getattr(signal, "SIGTERM", 15))
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=term_timeout)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
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
