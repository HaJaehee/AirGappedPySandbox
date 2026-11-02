"""Optional OS resource limits on kernel processes (``SANDBOX_ISOLATION=limits``).

The sandbox stays conventional (ADR-008): executed code can still read and
write whatever the user can. What this adds is availability. Every kernel gets
an OS ceiling its own code cannot lift, so a memory bomb, a fork bomb or a busy
loop in a thread ends inside that kernel instead of taking the host down with
it (ADR-023). Nothing here needs administrator rights or extra software.

- Windows: one unnamed Job Object per kernel, holding the committed memory of
  the kernel and its children, the number of live processes, a below-normal
  priority, no crash dialogs, and KILL_ON_JOB_CLOSE. Only this server holds the
  handle, so the kernel cannot loosen it, and the kernel's shutdown -- or this
  server's death -- takes every child process along.
- Linux: RLIMIT_DATA (soft and hard, so the kernel cannot raise it back) and
  nice 10, set from this process right after launch; children inherit both.
  The process count is not limited: RLIMIT_NPROC counts every process of the
  user, not of the kernel.

The limits are attached before the kernel has run a single line of its own:
on Windows the process is created suspended, on Linux it is still importing
ipykernel long before the first cell arrives.
"""

from __future__ import annotations

import os
import sys

import config

_OFF_VALUES = {"", "off", "0", "false", "no", "none"}


def _warn(message: str) -> None:
    # stderr only: stdout is the MCP stdio channel.
    print(f"[sandbox] isolation: {message}", file=sys.stderr, flush=True)


def _physical_mb() -> int | None:
    try:
        if os.name == "nt":
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return status.ullTotalPhys // (1024 * 1024)
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024)
    except (OSError, ValueError, AttributeError):
        return None


def memory_floor_mb() -> int:
    """Smallest per-kernel memory limit that still lets numpy import.

    OpenBLAS reserves its buffers per thread when numpy is imported -- about
    1.2 GB on a 20-thread machine, measured on Windows (commit charge) and on
    Linux (RLIMIT_DATA) alike -- and it ABORTS the process when it cannot. Below
    this floor the kernel dies at ``import numpy`` instead of raising
    MemoryError, which looks like a broken sandbox rather than a limit.
    """
    return 512 + 64 * (os.cpu_count() or 4)


def _resolve_level(raw: str) -> tuple[str, list[str]]:
    if raw in _OFF_VALUES:
        return "off", []
    if raw == "limits":
        return "limits", []
    # Fail towards protection: someone who wrote "on" or "strict" wanted MORE.
    return "limits", [f"SANDBOX_ISOLATION={raw!r} is not a known level; applied 'limits'"]


def _resolve_memory(raw: str) -> tuple[int | None, list[str]]:
    """Per-kernel memory limit in MB (None = no limit) and warnings about it.

    A share of the RAM ("25%", also the default) never goes below the numpy
    floor; a number of MB is taken as written, with a warning when it is lower.
    """
    floor = memory_floor_mb()
    value = raw or "25%"
    try:
        if not value.endswith("%"):
            mb = int(value)
            if mb <= 0:
                return None, []
            if mb < floor:
                return mb, [f"memory limit {mb} MB is below {floor} MB; `import numpy` may end the kernel"]
            return mb, []
        percent = float(value[:-1])
    except ValueError:
        return _resolve_memory("")[0], [f"SANDBOX_KERNEL_MEMORY_LIMIT={raw!r} is neither MB nor N%; used 25%"]
    if percent <= 0:
        return None, []
    physical = _physical_mb()
    if physical is None:
        return None, ["physical memory unknown; memory is not limited"]
    return max(int(physical * percent / 100), floor), []


LEVEL, _level_notes = _resolve_level(config.ISOLATION)
ACTIVE: bool = LEVEL == "limits"
MEMORY_MB, _memory_notes = _resolve_memory(config.KERNEL_MEMORY_LIMIT) if ACTIVE else (None, [])
# A venv python.exe is a launcher that starts the real interpreter, so a kernel
# is two processes from the start; a limit of 1 would stop it from booting.
MAX_PROCESSES: int = 0 if config.KERNEL_MAX_PROCESSES <= 0 else max(config.KERNEL_MAX_PROCESSES, 2)
NOTES: list[str] = _level_notes + _memory_notes

_LINUX = sys.platform.startswith("linux")


def supported() -> bool:
    return os.name == "nt" or _LINUX


def summary() -> str:
    """One line on what every kernel is held to, for logs and the pre-flight."""
    if not ACTIVE:
        return "isolation: off (conventional sandbox, no OS limits on kernels)"
    memory = f"{MEMORY_MB} MB" if MEMORY_MB else "unlimited"
    if os.name == "nt":
        processes = f"at most {MAX_PROCESSES} processes" if MAX_PROCESSES else "any number of processes"
        text = (
            f"isolation: limits -- each kernel in its own Job Object: memory {memory}, "
            f"{processes}, below-normal CPU priority; its child processes end with it"
        )
    elif _LINUX:
        text = (
            f"isolation: limits -- each kernel process: memory (RLIMIT_DATA) {memory}, "
            "nice 10; process count is not limited on Linux"
        )
    else:
        text = f"isolation: limits requested but not supported on {sys.platform}; kernels run WITHOUT limits"
    if NOTES:
        text += " [" + "; ".join(NOTES) + "]"
    return text


def memory_hint() -> str:
    """Sentence telling the model what its memory ceiling is, or ''."""
    if not ACTIVE or not MEMORY_MB or not supported():
        return ""
    return f"This kernel is limited to {MEMORY_MB} MB of memory (SANDBOX_KERNEL_MEMORY_LIMIT)."


def death_hint() -> str:
    """Likely cause of a kernel that died mid-cell, or ''."""
    if not ACTIVE or not MEMORY_MB or not supported():
        return ""
    return f" -- most likely it ran out of its {MEMORY_MB} MB memory limit"


# --- launching ------------------------------------------------------------------

def launch_kwargs() -> dict:
    """Extra Popen arguments for the kernel (start_kernel passes them through).

    On Windows the kernel is created suspended so it is inside its Job Object
    before it runs at all: a venv python.exe starts the real interpreter within
    milliseconds, and a child created before the assignment would escape it.
    """
    if ACTIVE and os.name == "nt":
        return {"creationflags": _CREATE_SUSPENDED}
    return {}


def attach(process) -> object | None:
    """Put a just-launched kernel under the limits; returns a token for release().

    Never raises for a limit that could not be applied -- it warns and the
    kernel runs without it. It does raise when a suspended Windows kernel cannot
    be resumed, after killing it, because that kernel would never answer.
    """
    if not ACTIVE:
        return None
    if os.name == "nt":
        return _attach_job(process)
    if _LINUX:
        _attach_rlimits(process.pid)
    return None


def release(token) -> None:
    """End a kernel's leftover child processes and drop its limits."""
    if token is None or os.name != "nt":
        return  # on Linux the kernel's process group is killed by jupyter_client
    _k32.TerminateJobObject(token, 1)
    _k32.CloseHandle(token)


def job_pids(token) -> list[int]:
    """Process ids currently held by a kernel's Job Object (Windows; else [])."""
    if token is None or os.name != "nt":
        return []
    info = _ProcessIdList()
    if not _k32.QueryInformationJobObject(token, _JOB_PROCESS_ID_LIST, ctypes.byref(info), ctypes.sizeof(info), None):
        return []
    return [int(info.ProcessIdList[i]) for i in range(info.NumberOfProcessIdsInList)]


# --- Linux ------------------------------------------------------------------------

def _attach_rlimits(pid: int) -> None:
    import resource

    try:
        if MEMORY_MB:
            limit = MEMORY_MB * 1024 * 1024
            resource.prlimit(pid, resource.RLIMIT_DATA, (limit, limit))
        # Only ever lower the priority: raising it back needs privileges.
        os.setpriority(os.PRIO_PROCESS, pid, max(os.getpriority(os.PRIO_PROCESS, pid), 10))
    except OSError as exc:
        _warn(f"kernel {pid} runs WITHOUT limits: {exc}")


# --- Windows ----------------------------------------------------------------------

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _CREATE_SUSPENDED = 0x00000004
    _JOB_PROCESS_ID_LIST = 3            # JobObjectBasicProcessIdList
    _JOB_EXTENDED_LIMITS = 9            # JobObjectExtendedLimitInformation
    _LIMIT_ACTIVE_PROCESS = 0x00000008
    _LIMIT_PRIORITY_CLASS = 0x00000020
    _LIMIT_JOB_MEMORY = 0x00000200
    _LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
    _LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimits(ctypes.Structure):
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

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _ProcessIdList(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * 256),
        ]

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    _k32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    _k32.OpenThread.restype = wintypes.HANDLE
    _k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.ResumeThread.restype = wintypes.DWORD
    _k32.ResumeThread.argtypes = [wintypes.HANDLE]

    def _new_job():
        job = _k32.CreateJobObjectW(None, None)  # unnamed: nothing else can open it
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _ExtendedLimits()
        basic = info.BasicLimitInformation
        basic.LimitFlags = _LIMIT_PRIORITY_CLASS | _LIMIT_DIE_ON_UNHANDLED_EXCEPTION | _LIMIT_KILL_ON_JOB_CLOSE
        basic.PriorityClass = _BELOW_NORMAL_PRIORITY_CLASS
        if MEMORY_MB:
            basic.LimitFlags |= _LIMIT_JOB_MEMORY
            info.JobMemoryLimit = MEMORY_MB * 1024 * 1024
        if MAX_PROCESSES:
            basic.LimitFlags |= _LIMIT_ACTIVE_PROCESS
            basic.ActiveProcessLimit = MAX_PROCESSES
        if not _k32.SetInformationJobObject(job, _JOB_EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            _k32.CloseHandle(job)
            raise error
        return job

    def _resume(pid: int) -> None:
        """Resume every thread of a process created with CREATE_SUSPENDED."""
        snapshot = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            resumed = False
            more = _k32.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = _k32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    if thread:
                        resumed = _k32.ResumeThread(thread) != 0xFFFFFFFF or resumed
                        _k32.CloseHandle(thread)
                more = _k32.Thread32Next(snapshot, ctypes.byref(entry))
            if not resumed:
                raise OSError(f"no thread of process {pid} could be resumed")
        finally:
            _k32.CloseHandle(snapshot)

    def _attach_job(process):
        job = None
        try:
            job = _new_job()
            if not _k32.AssignProcessToJobObject(job, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError as exc:
            if job:
                _k32.CloseHandle(job)
            job = None
            _warn(f"kernel {process.pid} runs WITHOUT limits: {exc}")
        finally:
            try:
                _resume(process.pid)
            except OSError as exc:
                process.kill()
                if job:
                    _k32.CloseHandle(job)
                raise RuntimeError(f"could not resume the kernel process after attaching limits: {exc}") from exc
        return job
