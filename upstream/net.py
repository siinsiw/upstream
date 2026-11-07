"""Everything that talks to the network on the user's behalf: the one place proxy settings,
resumable downloads and the metered-connection / battery policy live.

Every other module opens URLs through urlopen() here, never through urllib directly."""
from __future__ import annotations

import ctypes
import hashlib
import http.client
import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .config import DOWNLOAD_DIR
from .errors import Cancelled, InstallError
from .i18n import N_

log = logging.getLogger("upstream.net")

USER_AGENT = "Upstream-Updater/1.0"
CHUNK = 256 * 1024
RETRY_DELAYS = (1.0, 3.0, 6.0)      # seconds to wait before retry 1, 2 and 3 of a transient failure
MIN_PROGRESS = 64 * 1024            # an attempt that got this far does not count towards the retry limit
STALE_PART_SECONDS = 7 * 24 * 3600
LOW_BATTERY_PERCENT = 30
YES_MEMORY_SECONDS = 300            # an "Update all" answer of yes lasts while the batch keeps going
NO_MEMORY_SECONDS = 30              # a no only has to outlast the cancelling of the rest of the batch

_settings = None


def configure(settings) -> None:
    """Called once at start-up so proxy and connection policies can read the user's settings."""
    global _settings, _opener_cache
    _settings = settings
    with _opener_lock:
        _opener_cache = None


# --- proxy -----------------------------------------------------------------------------------

_opener_lock = threading.Lock()
_opener_cache: tuple[tuple[str, str], urllib.request.OpenerDirector] | None = None


def _proxy_setting() -> tuple[str, str]:
    value = _settings.get("proxy", None) if _settings is not None else None
    if not isinstance(value, dict):
        return "system", ""
    mode = value.get("mode")
    url = str(value.get("url") or "").strip()
    if mode == "none":
        return "none", ""
    return ("custom", url) if mode == "custom" and url else ("system", "")


def _proxy_address(url: str) -> str:
    """The proxy URL with a scheme added when the user left it out ('host:8080', 'user:pass@host:8080')."""
    text = url if "://" in url else "http://" + url
    try:
        parts = urllib.parse.urlsplit(text)
        parts.port          # raises ValueError for a port that is not a number
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise InstallError("The proxy address in Settings isn't valid.")
    return text


def _build_opener(mode: str, url: str) -> urllib.request.OpenerDirector:
    if mode == "none":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if mode == "custom":
        proxy = _proxy_address(url)
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()    # system: environment variables, then the Windows registry


def _opener() -> urllib.request.OpenerDirector:
    global _opener_cache
    key = _proxy_setting()
    with _opener_lock:
        if _opener_cache is None or _opener_cache[0] != key:
            _opener_cache = (key, _build_opener(*key))
        return _opener_cache[1]


def urlopen(req, timeout: float = 20):
    """Open a Request or URL string through the proxy the user chose in Settings."""
    return _opener().open(req, timeout=timeout)


# --- metered connection and battery ----------------------------------------------------------

_CLSID_NETWORK_LIST_MANAGER = "DCB00C01-570F-4A9B-8D69-199FDBA5723B"
_IID_NETWORK_COST_MANAGER = "DCB00008-570F-4A9B-8D69-199FDBA5723B"
_CLSCTX_ALL = 0x17
# NLM_CONNECTION_COST (netlistmgr.h): 1 unrestricted, 2 fixed, 4 variable, then flags
_COST_METERED = 0x2 | 0x4 | 0x10000 | 0x40000     # fixed, variable, over the data limit, roaming


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str):
        u = uuid.UUID(text)
        super().__init__(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_uint32), ("BatteryFullLifeTime", ctypes.c_uint32)]


def _cost_is_metered(cost: int) -> bool | None:
    return None if cost == 0 else bool(cost & _COST_METERED)     # 0 is NLM_CONNECTION_COST_UNKNOWN


def _read_network_cost() -> int | None:
    """INetworkCostManager::GetCost for the current connection. Runs on a throwaway thread (see
    _call_with_timeout), so initialising COM here cannot disturb the caller's apartment."""
    if os.name != "nt":
        return None
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32,
                                       ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoUninitialize.restype = None
    if ole32.CoInitializeEx(None, 0) < 0:
        return None
    try:
        manager = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(_GUID(_CLSID_NETWORK_LIST_MANAGER)), None, _CLSCTX_ALL,
                                    ctypes.byref(_GUID(_IID_NETWORK_COST_MANAGER)), ctypes.byref(manager))
        if hr < 0 or not manager.value:
            return None
        vtable = ctypes.cast(manager, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        # IUnknown takes slots 0-2, so GetCost(DWORD *cost, NLM_SOCKADDR *destination) is slot 3
        get_cost = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                                      ctypes.c_void_p)(vtable[3])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
        cost = ctypes.c_uint32()
        try:
            return cost.value if get_cost(manager, ctypes.byref(cost), None) >= 0 else None
        finally:
            release(manager)
    finally:
        ole32.CoUninitialize()


def _call_with_timeout(fn, seconds: float):
    """fn() on a daemon thread; None if it raises or does not finish in time (fail open)."""
    box: list = []

    def run():
        try:
            box.append(fn())
        except Exception:
            log.debug("%s failed", fn.__name__, exc_info=True)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    return box[0] if box else None


def is_metered() -> bool | None:
    """True when Windows marks the current connection as metered (or roaming, or over its data
    limit), False when it does not, None when that cannot be told."""
    cost = _call_with_timeout(_read_network_cost, 5)
    return None if cost is None else _cost_is_metered(cost)


def _power_status() -> _PowerStatus | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_PowerStatus)]
    kernel32.GetSystemPowerStatus.restype = ctypes.c_int
    status = _PowerStatus()
    return status if kernel32.GetSystemPowerStatus(ctypes.byref(status)) else None


def battery_status() -> tuple[bool, int | None] | None:
    """(running on battery, charge percent or None when unknown); None on a PC without a battery
    or when the power state cannot be read."""
    status = _power_status()
    if status is None or status.BatteryFlag & 128 or status.ACLineStatus == 255:
        return None
    percent = status.BatteryLifePercent
    return status.ACLineStatus == 0, percent if percent <= 100 else None


def battery_saver() -> bool:
    """Windows battery saver is switched on."""
    status = _power_status()
    return bool(status and status.SystemStatusFlag & 1)


# What the user answered for the current "Update all" batch: kind -> (answer, valid until).
_answers: dict[str, tuple[bool, float]] = {}
_answers_lock = threading.Lock()


def _now() -> float:
    return time.monotonic()


def _remembered(kind: str) -> bool | None:
    with _answers_lock:
        entry = _answers.get(kind)
        if entry is None or entry[1] < _now():
            _answers.pop(kind, None)
            return None
        if entry[0]:
            _answers[kind] = (True, _now() + YES_MEMORY_SECONDS)
        return entry[0]


def _remember(kind: str, answer: bool) -> None:
    with _answers_lock:
        _answers[kind] = (answer, _now() + (YES_MEMORY_SECONDS if answer else NO_MEMORY_SECONDS))


def _policy(ctx, key: str) -> str:
    value = ctx.setting(key, "ask")
    return value if value in ("ask", "skip", "ignore") else "ask"


def _gate(ctx, kind: str, policy: str, question: str, detail: str, yes: str, refusal: str, values: dict) -> None:
    """Skip refuses outright; ask asks (once per "Update all" batch). A refusal raises Cancelled."""
    if policy == "ask":
        answer = _remembered(kind) if ctx.bulk else None
        if answer is None:
            answer = ctx.ask(question, detail=detail, yes=yes, no=N_("Not now"), **values)
            if ctx.bulk:
                _remember(kind, answer)
        if answer:
            return
    log.info("Update not started (%s policy: %s)", kind, policy)
    raise Cancelled(refusal, **values)


def preflight(ctx) -> None:
    """Run before a download starts: metered connection and battery policy.
    May ask the user through ctx.ask() or raise errors.Cancelled."""
    policy = _policy(ctx, "metered_policy")
    if policy != "ignore" and is_metered():
        _gate(ctx, "metered", policy,
              N_("This PC is on a metered connection. Download updates anyway?"),
              N_("Updates can be large and may count against your data plan."),
              N_("Download anyway"),
              N_("Not updated: this PC is on a metered connection."), {})
    policy = _policy(ctx, "battery_policy")
    status = battery_status() if policy != "ignore" else None
    if status and status[0]:
        detail = N_("If the power runs out in the middle of an installation, the app can be left broken. "
                    "Plugging in is safer.")
        percent = status[1]
        if percent is not None and percent <= LOW_BATTERY_PERCENT:
            _gate(ctx, "battery", policy,
                  N_("The battery is at {percent}% and the PC is not plugged in. Update anyway?"), detail,
                  N_("Update on battery"),
                  N_("Not updated: the battery is at {percent}% and the PC is not plugged in."),
                  {"percent": percent})
        elif battery_saver():
            _gate(ctx, "battery", policy,
                  N_("Battery saver is on and the PC is not plugged in. Update anyway?"), detail,
                  N_("Update on battery"),
                  N_("Not updated: battery saver is on and the PC is not plugged in."), {})


# --- download --------------------------------------------------------------------------------

class _Restart(Exception):
    """The kept partial file cannot be trusted: throw it away and download from byte 0."""


def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(". ") or "installer.exe"


def _name_from(resp) -> str:
    disposition = resp.headers.get("Content-Disposition") or ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition)
    return m.group(1) if m else resp.url.rsplit("/", 1)[-1].split("?", 1)[0]


def _int(value) -> int:
    return int(value) if value and str(value).isdigit() else 0


def _range_total(header: str | None) -> int:
    """The complete size from 'bytes 100-199/1234' or 'bytes */1234'; 0 when unknown."""
    m = re.fullmatch(r"\s*bytes\s+(?:\d+-\d+|\*)/(\d+|\*)\s*", header or "")
    return _int(m.group(1)) if m else 0


def _range_start(header: str | None) -> int:
    m = re.match(r"\s*bytes\s+(\d+)-\d+/", header or "")
    return int(m.group(1)) if m else -1


def _part_paths(url: str) -> tuple[Path, Path]:
    """The partial file and its sidecar, named after the URL so a retry finds them again before
    the server has said what the file is called."""
    part = DOWNLOAD_DIR / (hashlib.sha256(url.encode("utf-8")).hexdigest()[:16] + ".part")
    return part, part.with_name(part.name + ".json")


def _read_meta(path: Path, url: str) -> dict:
    try:
        meta = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) and meta.get("url") == url else {}


def _write_meta(path: Path, meta: dict) -> None:
    try:
        path.write_text(json.dumps(meta), "utf-8")
    except OSError as e:
        log.warning("Could not write %s, the download will not be resumable: %s", path.name, e)


def _validator(meta: dict) -> str:
    """What If-Range may carry: a strong ETag, else the Last-Modified date."""
    etag = meta.get("etag") or ""
    return etag if etag and not etag.startswith("W/") else meta.get("last_modified") or ""


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _discard(part: Path, meta_path: Path) -> None:
    for path in (part, meta_path):
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("Could not remove %s: %s", path.name, e)


def _purge_stale() -> None:
    """Interrupted downloads nobody resumed within a week."""
    cutoff = time.time() - STALE_PART_SECONDS
    for pattern in ("*.part", "*.part.json"):
        for path in DOWNLOAD_DIR.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    log.info("Removed stale partial download %s", path.name)
            except OSError:
                pass


def _save_error(e: OSError) -> InstallError:
    return InstallError("Could not save the download: {error}", error=str(e))


def _hash_prefix(digest, path: Path, size: int, cancel) -> None:
    """Feed the first `size` bytes of the kept partial file into `digest`."""
    try:
        with open(path, "rb") as fh:
            while size:
                chunk = fh.read(min(CHUNK, size))
                if not chunk:
                    raise _Restart()
                digest.update(chunk)
                size -= len(chunk)
                if cancel is not None:
                    cancel.check()
    except OSError as e:
        raise _save_error(e) from e


def _complete(part: Path, meta_path: Path, name: str, digest, expected_sha256: str | None,
              resumed: bool) -> Path:
    if expected_sha256 and digest.hexdigest().lower() != expected_sha256.lower():
        _discard(part, meta_path)
        if resumed:
            raise _Restart()        # the kept prefix may be the bad part: try once more from the start
        raise InstallError("The download's SHA-256 doesn't match the hash GitHub published; nothing was installed.")
    dest = DOWNLOAD_DIR / name
    try:
        os.replace(part, dest)
    except OSError as e:
        raise _save_error(e) from e
    meta_path.unlink(missing_ok=True)
    return dest


def _attempt(url: str, file_name: str | None, part: Path, meta_path: Path, on_progress,
             expected_sha256: str | None, cancel) -> Path:
    """One connection: continue the kept partial file when the server can, else start over."""
    meta = _read_meta(meta_path, url) if part.exists() else {}
    offset = _size(part) if meta else 0
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    validator = _validator(meta)
    if offset and validator:
        headers["Range"] = f"bytes={offset}-"
        headers["If-Range"] = validator
    else:
        offset = 0
    try:
        resp = urlopen(urllib.request.Request(url, headers=headers), timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416 and offset:
            size = _range_total(e.headers.get("Content-Range"))
            e.close()
            if size != offset:
                raise _Restart() from None
            digest = hashlib.sha256() if expected_sha256 else None
            if digest is not None:
                _hash_prefix(digest, part, offset, cancel)
            return _complete(part, meta_path, _safe_name(file_name or meta.get("name") or ""), digest,
                             expected_sha256, True)      # the kept file already is the whole file
        raise
    with resp:
        if resp.status == 206 and offset:
            size = _range_total(resp.headers.get("Content-Range"))
            etag = resp.headers.get("ETag") or ""
            if (_range_start(resp.headers.get("Content-Range")) != offset
                    or (size and meta.get("size") and size != meta["size"])
                    or (etag and meta.get("etag") and etag != meta["etag"])):
                raise _Restart()
            length = _int(resp.headers.get("Content-Length"))
            total = size or (offset + length if length else 0)
            name = _safe_name(file_name or meta.get("name") or _name_from(resp))
        elif resp.status == 200:
            offset = 0              # a fresh body, whether or not we asked for a range
            total = _int(resp.headers.get("Content-Length"))
            name = _safe_name(file_name or _name_from(resp))
            meta = {"url": url, "name": name, "etag": resp.headers.get("ETag") or "",
                    "last_modified": resp.headers.get("Last-Modified") or "", "size": total}
        else:
            raise InstallError("The server refused the download (HTTP {code}).", code=resp.status)
        digest = hashlib.sha256() if expected_sha256 else None
        if offset and digest is not None:
            _hash_prefix(digest, part, offset, cancel)
        try:
            fh = open(part, "ab" if offset else "wb")
        except OSError as e:
            raise _save_error(e) from e
        with fh:
            if not offset:
                _write_meta(meta_path, meta)
            done = offset
            on_progress(done, total)
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                try:
                    fh.write(chunk)
                except OSError as e:
                    raise _save_error(e) from e
                if digest is not None:
                    digest.update(chunk)
                done += len(chunk)
                on_progress(done, total)
                if cancel is not None:
                    cancel.check()
    if total and done != total:
        raise ConnectionError(N_("The connection closed before the whole file arrived"))
    return _complete(part, meta_path, name, digest, expected_sha256, offset > 0)


def _is_transient(e: Exception) -> bool:
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (408, 429) or e.code >= 500
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    return not isinstance(reason, ssl.SSLCertVerificationError)


def _gave_up(e: Exception) -> InstallError:
    if isinstance(e, urllib.error.HTTPError):
        return InstallError("The server is not answering properly (HTTP {code}). Try again later.", code=e.code)
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    return InstallError("The connection keeps failing, so the download stopped: {error}", error=str(reason))


def _pause(seconds: float, cancel) -> None:
    if cancel is None:
        time.sleep(seconds)
    elif cancel.wait(seconds):
        cancel.check()


def download(url: str, file_name: str | None, on_progress, expected_sha256: str | None = None,
             cancel=None) -> Path:
    """Download to the downloads folder and return the file. `cancel` is a context.CancelToken.

    The partial file stays on disk, with a sidecar holding the URL, validators and expected size,
    after a cancel or a failure; the next call for the same URL continues from it (Range plus
    If-Range). Transient failures are retried up to three times with a growing pause."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _purge_stale()
    part, meta_path = _part_paths(url)
    failures = 0
    while True:
        if cancel is not None:
            cancel.check()
        kept = _size(part)
        try:
            return _attempt(url, file_name, part, meta_path, on_progress, expected_sha256, cancel)
        except _Restart:
            log.info("Discarding the partial download of %s and starting over", url)
            _discard(part, meta_path)
        except (OSError, http.client.HTTPException) as e:
            if isinstance(e, urllib.error.HTTPError):
                e.close()
                if not _is_transient(e):
                    raise InstallError("The server refused the download (HTTP {code}).", code=e.code) from None
            elif not _is_transient(e):
                raise
            if _size(part) - kept >= MIN_PROGRESS:
                failures = 0
            if failures >= len(RETRY_DELAYS):
                raise _gave_up(e) from e
            delay = RETRY_DELAYS[failures]
            failures += 1
            log.warning("Download of %s failed (%s); retry %d of %d in %gs", url, e, failures,
                        len(RETRY_DELAYS), delay)
            _pause(delay, cancel)
