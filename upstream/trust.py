"""Publisher check: refuse or question an update signed by someone other than the app's usual publisher.

The usual publisher is, in order of trust:
  1. the certificate CN remembered after a verified install (settings["signers"]),
  2. the Authenticode signer of the installed app's own files,
  3. the registry "Publisher" text. Anyone can write that, so it can make Upstream ask but never block.
Certificate names (1 and 2) must match exactly once case, punctuation, word order and legal forms are ignored;
the registry text only has to be contained in the signer's name. A signer the user accepted after a warning
(settings["signers_approved"]) counts as the usual one for that app, without replacing the remembered CN.
A file without a signature is fine only for an app with no signed baseline (most open-source apps); with no
baseline, a signature Windows rejects (untrusted, broken, unsupported) is still questioned.
Owner: the publisher-signature feature."""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import installer
from .i18n import N_, english
from .inventory import InstalledApp

log = logging.getLogger("upstream.trust")

POLICIES = ("ask", "block", "off")
# Legal forms dropped before names are compared: "Google LLC" and "Google Inc." are the same publisher.
_LEGAL = {"inc", "incorporated", "llc", "ltd", "limited", "gmbh", "corp", "corporation", "co", "company",
          "ag", "sa", "bv", "ab", "oy", "plc", "pty", "srl", "sarl", "kg", "llp", "lp", "nv", "kk"}
_HELPER_EXE = re.compile(r"unins|uninst|setup|install|update|redist|crash|helper", re.I)

_cache: dict[tuple[str, str], str] = {}          # (reg_key, version) -> signer of the installed copy ('' = unsigned)
_seen: dict[tuple[str, str], str] = {}           # (ident, signer) -> Authenticode status evaluate() saw
_lock = threading.Lock()


@dataclass(frozen=True)
class Decision:
    action: str                    # allow | ask | block
    template: str = ""             # English question or reason
    values: dict = field(default_factory=dict)
    detail: str = ""               # English template for the second paragraph, same values


@dataclass(frozen=True)
class _Usual:
    name: str
    strong: bool                   # a certificate name (remembered or read from the installed files), not registry text


def _tokens(name: str) -> frozenset[str]:
    text = re.sub(r"[.'’]", "", (name or "").lower())
    return frozenset(re.findall(r"\w+", text)) - _LEGAL


def _same_publisher(usual: str, signer: str, strong: bool = True) -> bool:
    """Case, punctuation, word order and legal forms are ignored. Certificate names must then be equal, so
    'Microsoft Corporation Fake Ltd' is not 'Microsoft Corporation'. Registry text (strong=False) is only a hint
    and may be shorter than the certificate name: every word of it must appear in the signer's name."""
    x, y = _tokens(usual), _tokens(signer)
    if not (x and y):
        return False
    return x == y if strong else x <= y


def _approved(settings, ident: str) -> list[str]:
    """Signers the user accepted for this app after a warning (the key may be missing or damaged in old files)."""
    data = settings.get("signers_approved", {}) if settings is not None else {}
    names = data.get(ident) if isinstance(data, dict) else None
    return [n.strip() for n in names if isinstance(n, str) and _tokens(n)] if isinstance(names, list) else []


def _policy(settings) -> str:
    value = settings.get("publisher_policy", "ask") if settings is not None else "ask"
    return value if value in POLICIES else "ask"


def _signers(settings) -> dict:
    data = settings.get("signers", {}) if settings is not None else {}
    return data if isinstance(data, dict) else {}


def _program(command: str) -> str:
    """The executable of a registry command line, quoted or not ('C:\\Program Files\\X\\uninst.exe /S')."""
    text = os.path.expandvars((command or "").strip())
    if text.startswith('"'):
        return text[1:].split('"', 1)[0]
    for stop in [m.start() for m in re.finditer(r"\s", text)] + [len(text)]:
        if os.path.isabs(text[:stop]) and os.path.isfile(text[:stop]):
            return text[:stop]
    return ""


def _signable(path: str) -> bool:
    """A file worth reading a signature from. Anything under the Windows folder is signed by Microsoft
    whatever app points at it (msiexec.exe, shell32.dll), so it says nothing about the app."""
    if not path or not os.path.isabs(path) or not path.lower().endswith((".exe", ".dll")) or not os.path.isfile(path):
        return False
    windows = os.environ.get("SystemRoot")
    return not (windows and os.path.normcase(os.path.abspath(path)).startswith(
        os.path.normcase(os.path.abspath(windows)) + os.sep))


def _named_exe(location: str, name: str) -> str:
    """An executable in the install folder whose file name looks like the app's name ('' when none does)."""
    key = re.sub(r"[\W_]+", "", name.lower())
    try:
        entries = sorted((e for e in os.scandir(location) if e.is_file() and e.name.lower().endswith(".exe")),
                         key=lambda e: e.name.lower())
    except OSError:
        return ""
    for entry in entries:
        stem = re.sub(r"[\W_]+", "", entry.name[:-4].lower())
        if len(stem) >= 3 and not _HELPER_EXE.search(stem) and (stem in key or key in stem):
            return entry.path
    return ""


def _lookup(app: InstalledApp) -> tuple[str, bool]:
    """(signer, final) of the installed copy. final is False when PowerShell could not answer."""
    paths: list[str] = []
    for path in (app.icon_path, _named_exe(app.install_location, app.name) if app.install_location else "",
                 _program(app.uninstall_string)):
        if _signable(path) and path not in paths:
            paths.append(path)
    for path in paths:
        try:
            status, signer = installer.signature(Path(path))
        except Exception:    # a best-effort probe of an external tool must never break the update
            log.warning("could not read the signature of %s", path, exc_info=True)
            return "", False
        if status == "Valid" and signer:
            return signer, True
        if status == "Unknown":
            return "", False
    return "", True


def _installed_signer(app: InstalledApp) -> str:
    """Who signed the installed app ('' when nothing found is signed). Cached per app and version, so
    an unsigned app costs its few PowerShell starts only once."""
    key = (app.reg_key, app.version)
    if key not in _cache:
        signer, final = _lookup(app)
        if not final:
            return signer
        _cache[key] = signer
    return _cache[key]


def _expected(settings, app: InstalledApp) -> _Usual | None:
    remembered = str(_signers(settings).get(app.ident) or "").strip()
    if _tokens(remembered):
        return _Usual(remembered, True)
    installed = _installed_signer(app)
    if _tokens(installed):
        return _Usual(installed, True)
    if _tokens(app.publisher):
        return _Usual(app.publisher.strip(), False)
    return None


# Second paragraph shared by the "Windows couldn't verify the signature" questions.
_UNVERIFIED_DETAIL = N_(
    "The certificate may be untrusted, revoked or expired, or the file may have been altered. If you didn't "
    "expect this, choose “Don't install” and get the update from the app's official website.")


def _judge(policy: str, usual: _Usual | None, app: InstalledApp, status: str, signer: str,
           approved: tuple[str, ...] = ()) -> Decision:
    signed = status == "Valid" and bool(signer)
    strong = usual is not None and usual.strong
    if signed:
        if usual is None:
            log.info("%s: nothing known about its publisher (%s %s), allowing", app.name, status, signer)
            return Decision("allow")
        if _same_publisher(usual.name, signer, usual.strong):
            log.info("%s: signed by %s, the usual publisher (%s)", app.name, signer, usual.name)
            return Decision("allow")
        if any(_same_publisher(name, signer) for name in approved):
            log.info("%s: signed by %s, which you accepted earlier", app.name, signer)
            return Decision("allow")
        values = {"name": app.name, "usual": usual.name, "signer": signer}
        if policy == "block" and strong:
            return Decision("block", "Blocked: {name} is normally published by {usual}, but this file is signed by "
                                     "{signer}. Nothing was installed. You can change this in Settings.", values)
        return Decision(
            "ask", "{name} is normally published by {usual}, but this file is signed by {signer}.", values,
            "This can happen when a company is renamed or sold, but it can also mean the download was swapped. "
            "If you didn't expect it, choose “Don't install” and get the update from the app's official website.")

    if not strong:
        # Without a certificate name to compare with, only "no signature" and "could not ask" are normal: most
        # open-source apps are unsigned, and the registry text does not say whether the app was ever signed.
        if status == "Valid":
            log.info("%s: valid signature without a readable name and no signed baseline, allowing", app.name)
            return Decision("allow")
        if status == "NotSigned":
            log.info("%s: file not signed and no signed copy is installed, allowing", app.name)
            return Decision("allow")
        if status == "Unknown":
            log.warning("%s: the signature check itself failed and no signed copy is installed, allowing", app.name)
            return Decision("allow")

    block = policy == "block"
    values = {"name": app.name} if usual is None else {"name": app.name, "usual": usual.name}
    if status == "NotSigned":
        if block:
            return Decision("block", "Blocked: {name} is normally signed by {usual}, but this file isn't digitally "
                                     "signed. Nothing was installed. You can change this in Settings.", values)
        return Decision(
            "ask", "{name} is normally signed by {usual}, but this file isn't digitally signed.", values,
            "Without a signature there is no way to tell who made the file. If you didn't expect this, choose "
            "“Don't install” and get the update from the app's official website.")
    if usual is None:
        if block:
            return Decision("block", "Blocked: Windows couldn't verify the digital signature of this file for "
                                     "{name}. Nothing was installed. You can change this in Settings.", values)
        return Decision("ask", "Windows couldn't verify the digital signature of this file for {name}.", values,
                        _UNVERIFIED_DETAIL)
    if block:
        return Decision("block", "Blocked: {name} is normally signed by {usual}, but Windows couldn't verify this "
                                 "file's signature. Nothing was installed. You can change this in Settings.", values)
    return Decision(
        "ask", "{name} is normally signed by {usual}, but Windows couldn't verify this file's signature.", values,
        _UNVERIFIED_DETAIL)


def evaluate(settings, app: InstalledApp, sig_status: str, signer: str) -> Decision:
    """sig_status is Authenticode's status (Valid, NotSigned, ...); signer is the certificate's CN."""
    status = sig_status or "Unknown"
    signer = (signer or "").strip()
    _seen[(app.ident, signer)] = status
    policy = _policy(settings)
    if policy == "off":
        return Decision("allow")
    decision = _judge(policy, _expected(settings, app), app, status, signer, tuple(_approved(settings, app.ident)))
    if decision.action != "allow":
        log.warning("%s: %s - %s", app.name, decision.action, english(decision.template, decision.values))
    return decision


def remember(settings, app: InstalledApp, signer: str, sig_status: str | None = None, approved: bool = False) -> None:
    """Called after a successful install. Only a signature Windows reported as Valid counts; without sig_status,
    the status evaluate() saw for this file is used. Nothing is learned while the check is off, since then no
    file is being checked.
    approved=False: this signer is now the app's remembered publisher (a signer the user accepted earlier is
    never promoted to that, so it cannot push the real publisher out).
    approved=True: the user said yes to a warning. The signer is only added to settings["signers_approved"],
    so the remembered publisher stays what it was and the next update from the real one is not questioned."""
    signer = (signer or "").strip()
    seen = _seen.pop((app.ident, signer), None)
    if settings is None or not _tokens(signer) or (sig_status or seen) != "Valid" or _policy(settings) == "off":
        return
    with _lock:
        known = _approved(settings, app.ident)
        trusted = any(_same_publisher(name, signer) for name in known)
        if approved:
            if trusted:
                return
            everyone = settings.get("signers_approved", {})
            everyone = dict(everyone) if isinstance(everyone, dict) else {}
            everyone[app.ident] = [*known, signer]
            key, value = "signers_approved", everyone
        else:
            signers = dict(_signers(settings))
            if trusted or signers.get(app.ident) == signer:
                return
            signers[app.ident] = signer
            key, value = "signers", signers
        settings.set(key, value)
        try:
            settings.save()
        except OSError:
            log.warning("could not save the publisher of %s", app.name, exc_info=True)
