"""Save everything a pipeline prints to a log file in logs/ (at the repo root).

    from helpers.run_log import start_run_log
    start_run_log("download")      # -> logs/download_2026-11-07_14-30-00.log

After this call, everything written to stdout and stderr (print, tracebacks,
warnings, logging to stderr) still appears in the terminal and is also appended
to the file. Colour codes are stripped from the file. One file per run.

Limits: output that a child process writes straight to the terminal (not
through Python) is not captured. A failure to write the log (disk full,
read-only folder) is ignored on purpose: it must never stop a pipeline.
"""
from __future__ import annotations

import atexit
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_lock = threading.Lock()
_log_path: Path | None = None


class _Tee:
    """File-like wrapper: writes to the real stream and to the shared log file."""

    def __init__(self, stream, sink):
        self._stream = stream
        self._sink = sink

    def write(self, text):
        count = self._stream.write(text)
        try:
            with _lock:
                self._sink.write(ANSI_RE.sub("", text))
                self._sink.flush()
        except Exception:  # noqa: BLE001 - logging must never break the pipeline
            pass
        return count

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):  # isatty, fileno, encoding, ... come from the real stream
        return getattr(self._stream, name)


def start_run_log(name: str) -> Path | None:
    """Start mirroring stdout/stderr to logs/<name>_<timestamp>.log. Returns the path.

    Safe to call twice (the second call returns the first file). Returns None if
    the log folder or file cannot be created; the pipeline then runs without a log.
    """
    global _log_path
    if _log_path is not None:
        return _log_path
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        started = datetime.now()
        path = LOG_DIR / f"{name}_{started:%Y-%m-%d_%H-%M-%S}.log"
        sink = path.open("a", encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"could not create run log in {LOG_DIR}: {exc}", file=sys.stderr)
        return None

    command = ' '.join(getattr(sys, 'orig_argv', None) or sys.argv)  # e.g. python -m pkg.module --flag
    sink.write(f"# command: {command}\n# started: {started:%Y-%m-%d %H:%M:%S}\n\n")
    sink.flush()
    sys.stdout = _Tee(sys.stdout, sink)
    sys.stderr = _Tee(sys.stderr, sink)
    atexit.register(_finish, sink)
    _log_path = path
    print(f"[log] saving output to {path}", flush=True)
    return path


def _finish(sink) -> None:
    try:
        sink.write(f"\n# finished: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        sink.close()
    except Exception:  # noqa: BLE001
        pass
