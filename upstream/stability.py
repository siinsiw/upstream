"""Single instance, crash reports, log and download housekeeping. Owner: the stability feature.

Import-light on purpose (standard library only, Qt is imported inside the functions that need it):
main.py installs the crash handlers before the heavy imports, so a failing import is reported too."""
from __future__ import annotations

import atexit
import ctypes
import faulthandler
import functools
import getpass
import hashlib
import logging
import os
import platform
import re
import sys
import threading
import time
import traceback
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__
from .config import DATA_DIR, DOWNLOAD_DIR, LOG_FILE

log = logging.getLogger("upstream.stability")

DAY = 86400
STALE_DOWNLOAD_DAYS = 7
STALE_REPORT_DAYS = 30
KEEP_REPORTS = 5                # the newest reports stay however old they are
LOG_TAIL_LINES = 100
MAX_SCANNED = 10_000            # files looked at per folder by housekeeping
HANDOVER_TIMEOUT_MS = 5000      # how long a second launch waits for the first one to answer
HANDOVER_ATTEMPTS = 10          # while the first one is still opening its channel
HANDOVER_RETRY_MS = 100
MAX_CLIENTS = 16
QT_REPEATS = 3                  # an identical Qt message is logged this often, then summarised once

_ERROR_ALREADY_EXISTS = 183
_ERROR_INVALID_PARAMETER = 87
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_ACTIVATE, _PING = b"upstream-activate\n", b"upstream-ping\n"
_SEEN = ".reported.txt"       # a report the user has been told about

_mutex = 0                      # handle of the named mutex that makes this process "the" instance
_server = None                  # QLocalServer the other launches talk to
_fault_file = None
_installed = False
_reporting = False
_qt_handler = None
_qt_counts: dict[str, int] = {}


# -- single instance ---------------------------------------------------------
@functools.lru_cache(maxsize=None)
def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = wintypes.HANDLE
    k.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    return k


def instance_name() -> str:
    """One name per Windows user and session. A pipe name is machine-wide, so it has to carry both."""
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME') or getpass.getuser()}".lower()
    session = wintypes.DWORD(0)
    _k32().ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
    return f"Upstream-{hashlib.sha256(user.encode('utf-8')).hexdigest()[:12]}-{session.value}"


def _take_lock(name: str) -> tuple[str, int]:
    """("acquired", handle), ("held", 0) when another process has it, or ("denied", 0) when it has it at a
    higher privilege (an elevated Upstream): Windows then refuses to even open the mutex.
    The kernel drops the mutex with its process, so a crashed instance never leaves a stale lock."""
    k = _k32()
    handle = k.CreateMutexW(None, False, "Local\\" + name)
    error = ctypes.get_last_error()
    if not handle:
        return "denied", 0
    if error == _ERROR_ALREADY_EXISTS:
        k.CloseHandle(handle)
        return "held", 0
    return "acquired", handle


def _listen(name: str, on_activate: Callable[[], None]):
    """The channel later launches use to reach this instance. None when it cannot be opened: this instance
    still runs, a later launch just cannot hand over to it."""
    from PySide6.QtNetwork import QLocalServer
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.UserAccessOption)
    QLocalServer.removeServer(name)     # a leftover socket name; a Windows pipe goes away with its process
    if not server.listen(name):
        log.warning("Could not open the hand-over channel: %s", server.errorString())
        return None
    clients: dict = {}      # connected socket -> bytes received so far

    def read(sock):
        data = clients.get(sock, b"") + bytes(sock.readAll())
        line, newline, _ = data.partition(b"\n")
        if not newline:
            if len(data) > 64:
                sock.abort()
            else:
                clients[sock] = data
            return
        line += newline
        if line not in (_ACTIVATE, _PING):
            sock.abort()
            return
        if line == _ACTIVATE:
            try:
                on_activate()
            except Exception:
                log.exception("Could not show the window for a second launch")
        sock.write(b"ok\n")
        sock.flush()
        sock.disconnectFromServer()

    def drop(sock):
        clients.pop(sock, None)
        sock.deleteLater()

    def accept():
        while (sock := server.nextPendingConnection()) is not None:
            if len(clients) >= MAX_CLIENTS:
                sock.abort()
                sock.deleteLater()
                continue
            clients[sock] = b""
            sock.readyRead.connect(functools.partial(read, sock))
            sock.disconnected.connect(functools.partial(drop, sock))

    server.newConnection.connect(accept)
    return server


def _ask(name: str, message: bytes) -> str:
    """"ok" when the running instance answered, "silent" when it took the message but did not answer in time,
    "refused" when it could not be reached. Runs a nested event loop, so a server in this process works too."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtNetwork import QLocalSocket
    sock = QLocalSocket()
    loop = QEventLoop()
    state = {"done": False, "connected": False, "reply": b""}

    def finish(*_):
        state["done"] = True
        loop.quit()

    def connected():
        state["connected"] = True
        sock.write(message)

    def ready():
        state["reply"] += bytes(sock.readAll())
        if state["reply"].endswith(b"\n"):
            finish()

    sock.connected.connect(connected)
    sock.readyRead.connect(ready)
    sock.errorOccurred.connect(finish)
    sock.disconnected.connect(finish)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(finish)
    timer.start(HANDOVER_TIMEOUT_MS)
    sock.connectToServer(name)
    if not state["done"]:       # a refused connection has already finished by now
        loop.exec()
    timer.stop()
    state["reply"] += bytes(sock.readAll())
    sock.abort()
    if state["reply"].startswith(b"ok"):
        return "ok"
    return "silent" if state["connected"] else "refused"


def _pause(ms: int) -> None:
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _become_instance(handle: int, name: str, on_activate: Callable[[], None]) -> None:
    global _mutex, _server
    _mutex = handle
    _server = _listen(name, on_activate)


def acquire_single_instance(on_activate: Callable[[], None], *, name: str | None = None, activate: bool = True) -> bool:
    """True when this process is the only instance. False when another one is running: it has been told to
    show its window (its on_activate runs) and this process should exit. `activate=False` only checks, for a
    launch that must not pop the window up (--minimized). Needs a QApplication. Never raises: when the
    check itself fails the program starts normally."""
    try:
        return _acquire(on_activate, name or instance_name(), activate)
    except Exception:
        log.exception("Single-instance check failed; starting as a normal instance")
        return True


def _acquire(on_activate: Callable[[], None], name: str, activate: bool) -> bool:
    state, handle = _take_lock(name)
    if state == "acquired":
        _become_instance(handle, name, on_activate)
        return True
    # "held" can be a first instance that has not opened its channel yet, so retry for a moment; "denied"
    # is permanent.
    for attempt in range(HANDOVER_ATTEMPTS if state == "held" else 1):
        if attempt:
            _pause(HANDOVER_RETRY_MS)
            state, handle = _take_lock(name)    # the other one may have just gone
            if state == "acquired":
                _become_instance(handle, name, on_activate)
                return True
        outcome = _ask(name, _ACTIVATE if activate else _PING)
        if outcome == "ok":
            log.info("Another Upstream is already running; it was told to come forward")
            return False
        if outcome == "silent":
            log.warning("Another Upstream is running but did not answer in time; this launch ends")
            return False
    log.warning("Another Upstream holds the instance lock (%s) but cannot be reached, perhaps because it runs "
                "as administrator; starting as a normal instance", state)
    return True


def release_single_instance() -> None:
    """Close the channel and drop the lock. A process end does this by itself."""
    global _mutex, _server
    server, _server = _server, None
    if server is not None:
        server.close()
        server.deleteLater()
    handle, _mutex = _mutex, 0
    if handle:
        _k32().CloseHandle(handle)


def wait_for_exit(pid: int, timeout: float = 15.0) -> bool:
    """True once process `pid` is gone (or never existed), False when it still runs after `timeout` seconds.
    Used by the administrator relaunch, whose new process must not meet the old one's lock."""
    if pid <= 0:
        return True
    k = _k32()
    handle = k.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_INVALID_PARAMETER
    try:
        return k.WaitForSingleObject(handle, int(timeout * 1000)) == _WAIT_OBJECT_0
    finally:
        k.CloseHandle(handle)


# -- crash reports -----------------------------------------------------------
def crash_dir() -> Path:
    return DATA_DIR / "crashes"


_USER_DIR = re.compile(r"([\\/]+Users[\\/]+)([^\\/:*?\"<>|\r\n]+)", re.IGNORECASE)
_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")


def _user_names() -> list[str]:
    names = {os.environ.get("USERNAME", ""), Path.home().name}
    try:
        names.add(getpass.getuser())
    except Exception:
        pass
    return [n for n in names if len(n) >= 3]


def scrub(text: str) -> str:
    """The Windows user name out of every path, and no access token that slipped into a message."""
    text = _USER_DIR.sub(r"\1<user>", text)
    for name in _user_names():     # a profile outside C:\Users
        text = re.sub(rf"(?<=[\\/]){re.escape(name)}(?=[\\/])", "<user>", text, flags=re.IGNORECASE)
    return _TOKEN.sub("<token>", text)


def log_tail(lines: int = LOG_TAIL_LINES) -> list[str]:
    try:
        with open(LOG_FILE, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - 128 * 1024))
            data = fh.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-lines:]


def _qt_versions() -> str:
    try:
        import PySide6
        from PySide6.QtCore import qVersion
        return f"{qVersion()} (PySide6 {PySide6.__version__})"
    except Exception:
        return "unavailable"


def build_report(headline: str, details: str, when: float | None = None) -> str:
    """The text of a report: versions, the details (a traceback or a native stack dump) and the end of the
    log. It names no installed app except through log lines, and the user name is scrubbed from paths."""
    when = time.time() if when is None else when
    text = "\n".join([
        "Upstream crash report", headline, "",
        f"Upstream: {__version__} ({'exe' if getattr(sys, 'frozen', False) else 'source'})",
        f"Time: {datetime.fromtimestamp(when).astimezone().isoformat(timespec='seconds')}",
        f"Python: {platform.python_version()} ({platform.architecture()[0]})",
        f"Qt: {_qt_versions()}",
        f"Windows: {platform.platform()}", "",
        details.rstrip(), "",
        f"Last {LOG_TAIL_LINES} log lines:", *log_tail(),
    ])
    return scrub(text) + "\n"


def write_report(headline: str, details: str, when: float | None = None, pid: int | None = None) -> Path | None:
    """Save a report in the crash folder. None when that is not possible (a full disk must not hide the error)."""
    when = time.time() if when is None else when
    try:
        folder = crash_dir()
        folder.mkdir(parents=True, exist_ok=True)
        base = f"crash-{time.strftime('%Y%m%d-%H%M%S', time.localtime(when))}-{pid or os.getpid()}"
        path, n = folder / f"{base}.txt", 1
        while path.exists():
            n += 1
            path = folder / f"{base}_{n}.txt"
        path.write_text(build_report(headline, details, when), "utf-8")
        return path
    except OSError:
        return None


def _event_loop_running() -> bool:
    try:
        from PySide6.QtCore import QCoreApplication, QThread
        return QCoreApplication.instance() is not None and QThread.currentThread().loopLevel() > 0
    except Exception:
        return False


def _on_exception(exc_type, exc, tb, thread: str = "") -> None:
    """An exception nobody caught. Raised from a slot or a worker thread the program carries on, so that goes
    to the log only; one that leaves the program (no event loop running) also gets a report to show next time."""
    global _reporting
    if _reporting or issubclass(exc_type, KeyboardInterrupt):
        return
    _reporting = True
    try:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        if thread or _event_loop_running():
            log.error("Unhandled exception%s\n%s", f" in thread {thread}" if thread else "", text)
        else:
            path = write_report("An unhandled exception ended Upstream.", text)
            log.critical("Unhandled exception, Upstream is ending. Report: %s\n%s", path or "not saved", text)
    except Exception:
        pass
    finally:
        _reporting = False


def _log_qt(level: int, message: str) -> None:
    seen = _qt_counts.get(message, 0) + 1
    if message in _qt_counts or len(_qt_counts) < 256:
        _qt_counts[message] = seen
    if seen <= QT_REPEATS:
        logging.getLogger("upstream.qt").log(level, "%s", message)
    elif seen == QT_REPEATS + 1:
        logging.getLogger("upstream.qt").log(level, "%s (repeated; further copies are not logged)", message)


def _install_qt_handler() -> None:
    global _qt_handler
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    levels = {QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
              QtMsgType.QtFatalMsg: logging.CRITICAL}

    def handler(kind, context, message):
        try:
            if sys.stderr:      # Qt's own handler printed everything; keep that when run from a console
                print(message, file=sys.stderr)
            if kind in levels:
                _log_qt(levels[kind], message)
        except Exception:
            pass

    _qt_handler = handler       # Qt does not own it
    qInstallMessageHandler(handler)


def _close_fault_file() -> None:
    """A clean exit leaves no trace: the file only stays when something was written into it."""
    global _fault_file
    fh, _fault_file = _fault_file, None
    if fh is None:
        return
    try:
        faulthandler.disable()
        fh.close()
        if os.path.getsize(fh.name) == 0:
            os.unlink(fh.name)
    except (OSError, ValueError):
        pass


def exit_now(code: int) -> None:
    """Leave the process without interpreter finalisation. Worker threads (daemon) may still be inside a Qt call
    or about to emit a signal when a normal shutdown tears Qt down, which aborts the process; the next start would
    report that as a crash. Everything that must be saved was saved by then (the aboutToQuit handlers)."""
    _close_fault_file()
    try:
        logging.shutdown()
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.flush()
    except (OSError, ValueError):
        pass
    _flush_clipboard()
    os._exit(int(code))


def _flush_clipboard() -> None:
    """Qt hands text copied from Upstream to Windows lazily (OLE delayed rendering) and renders it in its own
    teardown, which os._exit skips: without this the report or command the user just copied is empty after quit."""
    try:
        ctypes.windll.ole32.OleFlushClipboard()
    except (OSError, AttributeError, ValueError):
        pass


def _enable_faulthandler() -> None:
    """Hard crashes (an access violation inside Qt, a fatal Python error) write their stack into this
    process's own file; the next start turns a non-empty one into a report."""
    global _fault_file
    folder = crash_dir()
    folder.mkdir(parents=True, exist_ok=True)
    _fault_file = open(folder / f"fault-{os.getpid()}.log", "ab", buffering=0)
    faulthandler.enable(_fault_file, all_threads=True)
    atexit.register(_close_fault_file)


def _adopt_fault_files() -> None:
    """Reports for the hard crashes of earlier runs. Done before this run logs anything, so the log tail
    is still the crashed run's. A file another running instance holds open cannot be removed and is left."""
    own = f"fault-{os.getpid()}.log"
    for path in sorted(crash_dir().glob("fault-*.log")):
        if path.name == own:
            continue
        try:
            dump = path.read_text("utf-8", errors="replace").strip()
            if dump:
                pid = re.search(r"-(\d+)\.log$", path.name)
                saved = write_report("Upstream crashed hard (a fault inside Qt or Python, not a Python error). "
                                     "Recovered at the next start, so the versions below are from that start.",
                                     dump, path.stat().st_mtime, int(pid.group(1)) if pid else None)
                if saved is None:
                    continue        # keep the dump for the next try
            path.unlink()
        except OSError:
            continue


def install_crash_handlers() -> None:
    """Uncaught exceptions (main and worker threads) and hard crashes are written to a report file.
    Safe to call twice, and never raises."""
    global _installed
    if _installed:
        return
    _installed = True
    previous, previous_thread = sys.excepthook, threading.excepthook

    def excepthook(exc_type, exc, tb):
        _on_exception(exc_type, exc, tb)
        previous(exc_type, exc, tb)

    def thread_hook(args):
        if args.exc_type is not SystemExit:
            _on_exception(args.exc_type, args.exc_value, args.exc_traceback, args.thread.name if args.thread else "?")
        previous_thread(args)

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    for step in (_adopt_fault_files, _enable_faulthandler, _install_qt_handler):
        try:
            step()
        except Exception:
            log.exception("Crash handler step %s failed", step.__name__)


def pending_crash_report() -> Path | None:
    """A report from an earlier run the user has not been told about yet, else None."""
    found = _reports(pending_only=True)
    return found[-1] if found else None


def _reports(pending_only: bool = False) -> list[Path]:
    try:
        files = sorted(crash_dir().glob("crash-*.txt"))
    except OSError:
        return []
    return [p for p in files if not (pending_only and p.name.endswith(_SEEN))]


def mark_reported(path: Path) -> None:
    """The user has been told. Older reports still pending count as told too: the dialog showed the newest
    one and the folder keeps the rest."""
    for p in {path, *(q for q in _reports(pending_only=True) if q.name <= path.name)}:
        if p.name.endswith(_SEEN):
            continue
        try:
            p.replace(p.with_name(p.name[:-len(".txt")] + _SEEN))
        except FileNotFoundError:
            continue        # already gone or already marked: nothing left to tell anyone about
        except OSError as e:
            log.warning("Could not mark %s as reported: %s", p.name, e)


# -- housekeeping ------------------------------------------------------------
def _purge_older_than(folder: Path, cutoff: float) -> int:
    """Delete the regular files directly in `folder` last written before `cutoff`. Links, junctions and
    subfolders are left alone, and so is a file something still has open (Windows refuses to delete it)."""
    removed = 0
    try:
        with os.scandir(folder) as entries:
            for n, entry in enumerate(entries):
                if n >= MAX_SCANNED:
                    break
                try:
                    if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.path)
                        removed += 1
                except OSError:
                    continue
    except OSError:
        pass        # the folder does not exist yet
    return removed


def _purge_reports(cutoff: float) -> int:
    removed = 0
    for path in _reports()[::-1][KEEP_REPORTS:]:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def housekeeping(now: float | None = None) -> None:
    """Delete stale downloads and old crash reports (the log rotates by itself, see config.setup_logging).
    Never raises. It may touch a few thousand files, so the caller runs it off the UI thread."""
    now = time.time() if now is None else now
    try:
        downloads = _purge_older_than(DOWNLOAD_DIR, now - STALE_DOWNLOAD_DAYS * DAY)
        reports = _purge_reports(now - STALE_REPORT_DAYS * DAY)
        if downloads or reports:
            log.info("Housekeeping removed %d old download file(s) and %d old crash report(s)", downloads, reports)
    except Exception:
        log.exception("Housekeeping failed")
