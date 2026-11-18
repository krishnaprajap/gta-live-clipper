"""OS abstraction: binaries, process priority, graceful stop, fonts and
hardware-based limits. Works on Windows (primary target) and Linux."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

IS_WINDOWS = os.name == "nt"


def find_binary(name: str, env_var: str) -> str:
    """Explicit env var (set by the Windows setup script) > PATH > bare name."""
    explicit = os.environ.get(env_var)
    if explicit and Path(explicit).exists():
        return explicit
    return shutil.which(name) or name


def streamlink_cmd() -> list[str]:
    """Run streamlink from the same Python environment as the app, so it works
    even when the venv's Scripts/ folder is not on PATH (scheduled tasks)."""
    return [sys.executable, "-m", "streamlink"]


def streamlink_available() -> bool:
    try:
        import streamlink  # noqa: F401
        return True
    except ImportError:
        return False


# ---- priority -------------------------------------------------------------
def background_popen_kwargs() -> dict:
    """Extra Popen kwargs for heavy background work (encodes) started from the
    clip worker thread: lowest CPU priority on Windows. On Linux the clip
    thread's nice value is inherited instead (see pipeline._lower_priority)."""
    if IS_WINDOWS and threading.current_thread().name.startswith("clipwork"):
        return {"creationflags": subprocess.IDLE_PRIORITY_CLASS}
    return {}


def set_app_priority(level: str = "below_normal") -> str:
    """Lower the whole app's priority so the PC stays responsive. Child
    processes (streamlink, ffmpeg) inherit it."""
    if level in (None, "", "normal"):
        return "normal"
    try:
        import psutil
        p = psutil.Process()
        if IS_WINDOWS:
            p.nice({"below_normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
                    "idle": psutil.IDLE_PRIORITY_CLASS}.get(level, psutil.BELOW_NORMAL_PRIORITY_CLASS))
        else:
            p.nice({"below_normal": 5, "idle": 15}.get(level, 5))
        return level
    except Exception as e:  # never fatal
        return f"unchanged ({e})"


def stop_process(p: subprocess.Popen) -> None:
    """Graceful stop: SIGINT on POSIX (ffmpeg finalises its output). Windows
    has no SIGINT for child processes, so terminate; finished segments are
    already on disk and only the partial one is lost."""
    if p.poll() is not None:
        return
    if IS_WINDOWS:
        p.terminate()
    else:
        p.send_signal(signal.SIGINT)


# ---- fonts ----------------------------------------------------------------
def caption_font() -> str:
    return "Arial" if IS_WINDOWS else "DejaVu Sans"


def drawtext_font_arg() -> str:
    """drawtext needs a font file on Windows (no fontconfig guarantee)."""
    if IS_WINDOWS:
        windir = os.environ.get("WINDIR", r"C:\Windows")
        for name in ("arialbd.ttf", "arial.ttf", "segoeui.ttf"):
            f = Path(windir) / "Fonts" / name
            if f.exists():
                return "fontfile='" + ffmpeg_filter_path(f) + "'"
    return "font='DejaVu Sans'"


def ffmpeg_filter_path(p: Path | str) -> str:
    """Escape a path for use inside an ffmpeg filtergraph option value.
    Windows: C:\\a\\b.srt -> C\\:/a/b.srt"""
    s = str(Path(p).resolve()) if not isinstance(p, str) else p
    s = s.replace("\\", "/")
    return s.replace(":", "\\:").replace("'", "\\'")


# ---- hardware-based limits -----------------------------------------------
def hardware() -> dict:
    cores = os.cpu_count() or 2
    try:
        import psutil
        ram_gb = psutil.virtual_memory().total / 1024 ** 3
    except Exception:
        ram_gb = 4.0
    return {"logical_cores": cores, "ram_gb": round(ram_gb, 1)}


def auto_max_streams(cores: int, ram_gb: float, cap: int = 6) -> int:
    """Conservative: keep 2 logical cores + 4 GB RAM for Windows, the clip
    encoder and Whisper; each extra stream needs ~2 logical cores of headroom
    (decode/analysis spikes, chat) and ~1 GB."""
    by_cpu = max(1, (cores - 2) // 2)
    by_ram = max(1, int(ram_gb - 4))
    return max(1, min(by_cpu, by_ram, cap))


# ---- Windows Job Object: hard CPU/memory caps + no orphaned children -------
import ctypes  # noqa: E402

_JOB_HANDLE = None  # must stay open for the life of the process


class _BasicLimit(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION (64 bytes on x64)
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]


class _ExtendedLimit(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION (144 bytes on x64)
    _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _CpuRate(ctypes.Structure):  # JOBOBJECT_CPU_RATE_CONTROL_INFORMATION
    _fields_ = [("ControlFlags", ctypes.c_uint32), ("CpuRate", ctypes.c_uint32)]


JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION = 15
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4


def resolve_limits(resources: dict, hw: dict | None = None) -> tuple[float, int]:
    """(max_memory_gb, max_cpu_percent) with "auto" resolved from hardware."""
    hw = hw or hardware()
    mem = resources.get("max_memory_gb", "auto")
    if str(mem).lower() == "auto":
        mem = min(max(3.0, hw["ram_gb"] * 0.4), 10.0)
    cpu = int(resources.get("max_cpu_percent", 75))
    return round(float(mem), 1), max(10, min(cpu, 100))


def apply_resource_limits(resources: dict) -> str:
    """Windows only. Puts this process (and every child it starts) in a job
    that (1) is killed as a whole if the app dies, (2) is hard-capped to
    max_cpu_percent of total CPU, (3) cannot exceed max_memory_gb."""
    global _JOB_HANDLE
    if not IS_WINDOWS:
        return "skipped (not Windows; use nice/cgroups/Docker limits)"
    mem_gb, cpu_pct = resolve_limits(resources)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return f"failed: CreateJobObject error {ctypes.get_last_error()}"
    ext = _ExtendedLimit()
    ext.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_JOB_MEMORY
    ext.JobMemoryLimit = int(mem_gb * 1024 ** 3)
    notes = []
    if not k32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(ext), ctypes.sizeof(ext)):
        notes.append(f"memory limit error {ctypes.get_last_error()}")
    if cpu_pct < 100:
        rate = _CpuRate(JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP, cpu_pct * 100)
        if not k32.SetInformationJobObject(job, JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION, ctypes.byref(rate), ctypes.sizeof(rate)):
            notes.append(f"cpu cap error {ctypes.get_last_error()}")
    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        return f"failed: AssignProcessToJobObject error {ctypes.get_last_error()}"
    _JOB_HANDLE = job
    return f"job limits: memory {mem_gb} GB, CPU {cpu_pct}%" + (f" ({'; '.join(notes)})" if notes else "")
