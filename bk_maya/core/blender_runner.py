"""Run Blender as a background subprocess and stream progress to Qt callers.

The class :class:`BlenderJob` wraps a ``QProcess`` that executes
``blender --background --python <script> -- <json-args>``.  The background
script (see :mod:`bk_maya.scripts.bg_download`) must print progress lines
in the form::

    BK_PROGRESS <0..1> <message>
    BK_STATUS   <status-string>
    BK_DONE     <output-path>
    BK_ERROR    <message>

Anything else is treated as plain Blender log output.

Auto-detection of ``blender.exe`` (Blender 5.0+ required):

1. ``prefs.blender_exe`` if set and existing.
2. ``BLENDER_PATH`` env var.
3. ``shutil.which("blender")``.
4. Common install dirs on Windows / macOS / Linux (latest version first).

Use :func:`find_blender_executable` for a one-off lookup, or instantiate
:class:`BlenderJob` to run a script asynchronously.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from qtpy.QtCore import QObject, QProcess, Signal

from .prefs import prefs

log = logging.getLogger(__name__)

MIN_BLENDER_MAJOR = 5
"""Minimum supported Blender major version."""

_VERSION_RE = re.compile(r"Blender\s+(\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------

def _candidate_paths() -> list[str]:
    """Return platform-specific candidate paths, newest-version first."""
    out: list[str] = []
    if sys.platform == "win32":
        for root in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ):
            base = Path(root) / "Blender Foundation"
            if base.is_dir():
                versions = sorted(
                    (p for p in base.iterdir() if p.is_dir() and p.name.lower().startswith("blender")),
                    key=lambda p: p.name,
                    reverse=True,
                )
                out.extend(str(v / "blender.exe") for v in versions)
    elif sys.platform == "darwin":
        out.extend([
            "/Applications/Blender.app/Contents/MacOS/Blender",
            os.path.expanduser("~/Applications/Blender.app/Contents/MacOS/Blender"),
        ])
    else:  # Linux / other
        for d in ("/usr/bin", "/usr/local/bin", "/opt/blender/blender",
                  os.path.expanduser("~/blender/blender")):
            out.append(d if d.endswith("blender") else os.path.join(d, "blender"))
    return out


def find_blender_executable() -> str:
    """Locate a usable Blender executable.  Returns "" if none found."""
    # 1) User pref
    candidate = (prefs.blender_exe or "").strip()
    if candidate and os.path.isfile(candidate):
        return candidate

    # 2) Env var
    env = os.environ.get("BLENDER_PATH", "").strip()
    if env and os.path.isfile(env):
        return env

    # 3) PATH
    which = shutil.which("blender") or (shutil.which("blender.exe") if sys.platform == "win32" else None)
    if which:
        return which

    # 4) Common install dirs
    for p in _candidate_paths():
        if os.path.isfile(p):
            return p
    return ""


def query_blender_version(exe_path: str, *, timeout: float = 5.0) -> tuple[int, int, int] | None:
    """Run ``<blender> --version`` and parse the version triple.

    Returns ``None`` on failure.
    """
    if not exe_path or not os.path.isfile(exe_path):
        return None
    try:
        proc = subprocess.run(
            [exe_path, "--version"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        first_line = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
        m = _VERSION_RE.search(first_line)
        if not m:
            return None
        return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def version_meets_min(version: tuple[int, int, int] | None) -> bool:
    return bool(version) and version[0] >= MIN_BLENDER_MAJOR


# ---------------------------------------------------------------------------
# Background job
# ---------------------------------------------------------------------------

class BlenderJob(QObject):
    """Spawn Blender headless and stream parsed progress signals.

    Signals
    -------
    progress(float, str)
        ``0.0..1.0`` fraction + status message.
    status(str)
        High-level status string (``"downloading"``, ``"importing"``, …).
    finished(str)
        Output file path on success.
    failed(str)
        Error message on failure (process error or BK_ERROR line).
    log_line(str)
        Raw stdout/stderr line (useful for the developer console).
    """

    progress  = Signal(float, str)
    status    = Signal(str)
    finished  = Signal(str)
    failed    = Signal(str)
    log_line  = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._buffer = ""
        self._last_output_path = ""
        self._error_emitted = False

    # ------------------------------------------------------------------
    def start(self, script_path: str, script_args: list[str], *,
              blender_exe: str = "") -> bool:
        """Launch Blender.  Returns True on successful start.

        On failure the :pyattr:`failed` signal is emitted synchronously.
        """
        exe = blender_exe or find_blender_executable()
        if not exe:
            self.failed.emit(
                "Blender executable not found. Set the path in BlenderKit → "
                "Settings → Files → Blender Executable."
            )
            return False
        if not os.path.isfile(script_path):
            self.failed.emit(f"Background script missing: {script_path}")
            return False

        argv = ["--background", "--factory-startup",
                "--python", script_path, "--", *script_args]

        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.MergedChannels)
        self._proc.readyReadStandardOutput.connect(self._read_stdout)
        self._proc.errorOccurred.connect(self._on_error)
        self._proc.finished.connect(self._on_finished)

        log.info("Launching Blender: %s %s", exe, " ".join(argv))
        self._proc.start(exe, argv)
        if not self._proc.waitForStarted(5000):
            self.failed.emit(f"Failed to start Blender ({exe}).")
            return False
        return True

    # ------------------------------------------------------------------
    def cancel(self) -> None:
        if self._proc and self._proc.state() != QProcess.NotRunning:
            self._proc.kill()

    # ------------------------------------------------------------------
    # Stdout parsing
    # ------------------------------------------------------------------
    def _read_stdout(self) -> None:
        if not self._proc:
            return
        chunk = bytes(self._proc.readAllStandardOutput()).decode(errors="replace")
        self._buffer += chunk
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._handle_line(line.rstrip("\r"))

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        self.log_line.emit(line)
        # Blender's crash-handler prints these once per worker thread; capture
        # the first one as a user-facing failure and stop the process so we
        # don't flood the log with hundreds of identical lines.
        if not self._error_emitted and "EXCEPTION_ACCESS_VIOLATION" in line:
            self._error_emitted = True
            self.failed.emit(
                "Blender crashed (EXCEPTION_ACCESS_VIOLATION). The installed "
                "Blender version may be incompatible \u2014 Blender 5.0 or newer "
                "is required. Check Settings \u2192 Files."
            )
            if self._proc and self._proc.state() != QProcess.NotRunning:
                self._proc.kill()
            return
        if line.startswith("BK_PROGRESS "):
            parts = line.split(" ", 2)
            try:
                frac = max(0.0, min(1.0, float(parts[1])))
            except (IndexError, ValueError):
                return
            msg = parts[2] if len(parts) > 2 else ""
            self.progress.emit(frac, msg)
        elif line.startswith("BK_STATUS "):
            self.status.emit(line[len("BK_STATUS "):].strip())
        elif line.startswith("BK_DONE "):
            self._last_output_path = line[len("BK_DONE "):].strip()
        elif line.startswith("BK_ERROR "):
            self._error_emitted = True
            self.failed.emit(line[len("BK_ERROR "):].strip())

    # ------------------------------------------------------------------
    def _on_error(self, err) -> None:  # QProcess.ProcessError
        if not self._error_emitted:
            self._error_emitted = True
            self.failed.emit(f"Blender process error: {err}")

    def _on_finished(self, exit_code: int, _exit_status) -> None:
        # Drain any remaining buffered partial line
        if self._buffer:
            self._handle_line(self._buffer)
            self._buffer = ""

        if self._error_emitted:
            return
        if exit_code != 0:
            self.failed.emit(f"Blender exited with code {exit_code}.")
            return
        if not self._last_output_path:
            self.failed.emit("Blender finished but produced no output file.")
            return
        self.finished.emit(self._last_output_path)
