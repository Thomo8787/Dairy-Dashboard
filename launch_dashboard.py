"""Launch Thomasson Farms Dashboard locally and open the browser."""

from __future__ import annotations

import atexit
import ctypes
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

HOST_URL = "http://127.0.0.1:5000"

# Windows: kill child processes when this launcher exits / window is closed.
_JobHandle = None


def project_root() -> Path:
    """
    Resolve the real project folder.

    When packaged with PyInstaller, __file__ points at a temp extract dir
    (_MEI...), so use the folder that contains the .exe instead.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def pick_python(root: Path) -> Path:
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return venv_python
    if getattr(sys, "frozen", False):
        return Path("python")
    return Path(sys.executable)


def _windows_job_kill_on_close(proc: subprocess.Popen) -> None:
    """Attach child process to a Job Object so it dies with this launcher."""
    global _JobHandle

    kernel32 = ctypes.windll.kernel32
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError("CreateJobObjectW failed")

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    JobObjectExtendedLimitInformation = 9
    if not kernel32.SetInformationJobObject(
        handle,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        kernel32.CloseHandle(handle)
        raise OSError("SetInformationJobObject failed")

    if not kernel32.AssignProcessToJobObject(handle, int(proc._handle)):  # type: ignore[attr-defined]
        kernel32.CloseHandle(handle)
        raise OSError("AssignProcessToJobObject failed")

    _JobHandle = handle


def stop_process_tree(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        # /T kills the whole tree (python + flask reloader children, etc.)
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main() -> int:
    root = project_root()
    os.chdir(root)

    app_path = root / "app.py"
    python = pick_python(root)

    if not app_path.exists():
        print("Could not find app.py next to the launcher.")
        print(f"Looked in: {root}")
        print("Keep ThomassonFarmsDashboard.exe inside the Dairy Dashboard folder.")
        input("Press Enter to close...")
        return 1

    if isinstance(python, Path) and not python.exists() and str(python) != "python":
        print("Python was not found. Create the virtual environment first:")
        print(r'  cd "C:\Dairy Dashboard"')
        print(r"  .\setup.ps1")
        input("Press Enter to close...")
        return 1

    env = os.environ.copy()
    # Auto-reload Python on save. Templates/static already refresh on browser reload.
    env.setdefault("FLASK_USE_RELOADER", "true")
    env.setdefault("FLASK_DEBUG", "false")
    env.setdefault("TEMPLATES_AUTO_RELOAD", "true")

    print("Starting Thomasson Farms Dashboard...")
    print(f"Project: {root}")
    print(f"Python:  {python}")
    print(f"URL:     {HOST_URL}")
    print("Auto-reload is on — save a .py file and wait a second; no manual restart needed.")
    print("Leave this window open while using the dashboard.")
    print("Close this window to stop the server.\n")

    proc = subprocess.Popen(
        [str(python), str(app_path)],
        cwd=str(root),
        env=env,
    )

    try:
        if os.name == "nt":
            _windows_job_kill_on_close(proc)
    except Exception as exc:
        print(f"Warning: could not bind process cleanup job ({exc}).")
        print("Will still attempt to stop the server on exit.")

    atexit.register(stop_process_tree, proc)

    time.sleep(2.0)
    try:
        webbrowser.open(HOST_URL)
    except Exception as exc:
        print(f"Could not open browser automatically: {exc}")
        print(f"Open {HOST_URL} manually.")

    try:
        return proc.wait()
    except KeyboardInterrupt:
        print("\nStopping dashboard...")
        stop_process_tree(proc)
        return 0
    finally:
        stop_process_tree(proc)


if __name__ == "__main__":
    raise SystemExit(main())
