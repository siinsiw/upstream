"""Runs one update: the chosen source first, then the fallbacks in order.

Each source goes through the same steps, and each step lives in its own module:

    net.preflight        metered connection / battery policy
    net.download         resumable, proxy-aware download            (direct sources)
    trust.evaluate       is the signer the app's usual publisher?
    installer.detect     which installer framework the file is
    inplace.plan         switches that make it replace the installed copy
    running.close_...    close the app's open processes
    verify.snapshot      the PC's state, taken right before the installer starts (again after each busy wait)
    installer.install    run it, waiting without blocking the queue
    exitcodes.classify   what the exit code means
    verify.check         what really changed on the PC

What a source's outcome does to the run:

    ok, restart          verify.check decides (restart is exit 3010/1641). Installed, restart, side by side and unknown
                         end the run as done; unchanged ends it as a failure that keeps the update offered, without
                         trying another source (a setup window the user closed also exits 0). A pending-restart flag
                         that appeared meanwhile only turns "installed" into "restart"; it never rescues "unchanged"
    cancelled            ends the run; no other source starts
    busy                 the same installer is run again a few times while Windows finishes the other installation
                         (the cancel token ends the wait), then the run ends: another source would meet the same lock
    already_installed,   done only if verify.check finds the new version listed (never as "restart", and the publisher
    newer_present        is not remembered: nothing was installed); otherwise the next source is tried
    disk_full            ends the run; no other source has more room
    needs_admin, failed  the next source is tried, and the last error is what the row shows

The downloaded installer is deleted when its source ends, whatever the outcome, except after
errors.StoppedWaiting: Upstream gave up waiting for an installer that may still be running and holding the file.

Messages travel as (English template, values) pairs and are translated by the
UI when shown, so switching language mid-install re-renders them correctly."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import exitcodes, inplace, installer, net, running, trust, verify, winget
from .context import UpdateContext
from .exitcodes import Outcome
from .i18n import SOURCE_LABELS, english
from .scanner import AppStatus, Update

log = logging.getLogger("upstream.update")

BUSY_RETRIES = 6            # times the installer is run again while another installation holds Windows Installer
BUSY_WAIT_SECONDS = 20
_APPLIED = ("installed", "restart", "side_by_side")   # verdicts that show the new version is on the PC
_NO_FALLBACK = ("busy", "disk_full")                  # no other source would fare better

KIND_LABELS = {
    "msi": "MSI", "msix": "MSIX", "inno": "Inno Setup", "nullsoft": "NSIS", "burn": "WiX Burn",
    "installshield": "InstallShield", "advancedinstaller": "Advanced Installer", "squirrel": "Squirrel",
    "exe": "unknown (runs its normal setup window)",
}


@dataclass
class Attempt:
    """What one source did: the outcome, the PC state taken right before its installer ran, and what the
    publisher check learned about the file (for trust.remember)."""
    outcome: Outcome
    before: verify.Snapshot = field(default_factory=verify.Snapshot)
    signer: str = ""
    sig_status: str = ""
    approved: bool = False         # the user answered Yes to a publisher warning for this file


def _discard(path: Path) -> None:
    """Delete a downloaded installer. A file an antivirus scanner or the installer itself still holds stays
    behind; that must never change how the install ended."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("could not delete %s", path, exc_info=True)


class Updater:
    def __init__(self, settings=None):
        self.settings = settings

    def run(self, status: AppStatus, progress, ctx: UpdateContext | None = None) -> tuple[bool, str, dict, str]:
        """progress(template, percent, **values), percent -1 meaning busy.
        Returns (ok, message template, message values, installed version)."""
        app = status.app
        ctx = ctx or UpdateContext(self.settings)
        attempts = [u for u in [status.update, *status.fallbacks] if u and (u.download_url or u.source == "winget")]
        if not attempts:
            return False, "No source for an automatic download", {}, app.version
        try:
            net.preflight(ctx)
        except installer.Cancelled as e:
            return False, e.template, e.values, app.version

        error = ("Install failed", {})
        for index, upd in enumerate(attempts):
            try:
                ctx.cancel.check()
                if upd.source == "winget":
                    done = self._winget(app, upd, progress, ctx)
                else:
                    done = self._direct(app, upd, progress, ctx)
            except installer.Cancelled as e:
                return False, e.template, e.values, app.version
            except installer.InstallError as e:
                done = Attempt(Outcome("failed", e.template, e.values))
            except Exception as e:
                log.exception("update of %s via %s failed", app.name, upd.source)
                done = Attempt(Outcome("failed", "{error}", {"error": str(e)}))
            outcome = done.outcome

            if outcome.cancelled:
                # The user stopped it (closed the installer, declined UAC): do not start another source.
                return False, outcome.template or "Cancelled.", outcome.values, app.version
            if outcome.ok or outcome.present:
                verdict = verify.check(app, done.before, outcome, upd.version, cancel=ctx.cancel)
                log.info("%s: %s (%s)", app.name, verdict.kind, english(verdict.template, verdict.values))
                if outcome.ok or verdict.kind in _APPLIED:
                    if outcome.ok and done.signer and verdict.kind in ("installed", "restart"):
                        self._remember(ctx, app, done)
                    return verdict.ok, verdict.template, verdict.values, verdict.version
            error = (outcome.template, outcome.values)
            log.warning("%s: %s failed: %s", app.name, upd.source, english(*error))
            if outcome.kind in _NO_FALLBACK:
                break
            if index + 1 < len(attempts):
                progress("{source} failed, trying the next source…", -1,
                         source=SOURCE_LABELS.get(upd.source, upd.source))
        return False, error[0], error[1], app.version

    @staticmethod
    def _remember(ctx: UpdateContext, app, done: Attempt) -> None:
        """The install worked, so its signer may become the app's usual publisher. trust decides whether this
        signature is one it may learn (a Valid one, or one the user approved); a failure here never changes
        the result of the install."""
        try:
            trust.remember(ctx.settings, app, done.signer, done.sig_status, approved=done.approved)
        except Exception:
            log.exception("%s: could not remember the publisher", app.name)

    def _direct(self, app, upd: Update, progress, ctx: UpdateContext) -> Attempt:
        progress("Downloading", 0)

        def on_bytes(done, total):
            progress("Downloading", int(done * 100 / total) if total else -1)

        path = net.download(upd.download_url, upd.file_name, on_bytes, upd.sha256, cancel=ctx.cancel)
        log.info("%s: downloaded %s (sha256 %s)", app.name, path.name,
                 "verified" if upd.sha256 else "not published")
        stopped = False
        try:
            return self._install_file(app, upd, path, progress, ctx)
        except installer.StoppedWaiting:
            stopped = True              # the installer may still be running, and it needs its file
            raise
        finally:
            if not stopped:
                _discard(path)

    def _install_file(self, app, upd: Update, path: Path, progress, ctx: UpdateContext) -> Attempt:
        progress("Checking signature", -1)
        sig_status, signer = installer.signature(path)
        log.info("%s: Authenticode %s %s", app.name, sig_status, signer)
        if sig_status == "HashMismatch":
            raise installer.InstallError("The file's digital signature is broken (tampered); nothing was installed.")

        decision = trust.evaluate(ctx.settings, app, sig_status, signer)
        if decision.action == "block":
            raise installer.InstallError(decision.template, **decision.values)
        approved = decision.action == "ask"
        if approved and not ctx.ask(decision.template, detail=decision.detail, danger=True,
                                    yes="Install anyway", no="Don't install", **decision.values):
            raise installer.Cancelled("Not installed: you declined a file that wasn't signed by the app's "
                                      "usual publisher.")

        kind = installer.detect(path, upd.installer_type)
        log.info("%s: installer type %s", app.name, KIND_LABELS.get(kind, kind))
        plan = inplace.plan(app, kind, path)
        for template, values in plan.warnings:
            log.warning("%s: %s", app.name, english(template, values))
        if plan.refuse:
            raise installer.InstallError(plan.refuse[0], **plan.refuse[1])

        ctx.cancel.check()              # a cancelled job must not close the user's app
        running.close_for_update(ctx, app, progress)

        before = verify.Snapshot()

        def attempt() -> Outcome:
            nonlocal before
            progress("Installing" if kind != "exe" else "Setup window opened", -1)
            # Taken for every run, right before the installer starts, not before the download, the questions or a
            # busy wait: a run that ended busy installed nothing, and a restart flag that Windows Update (say)
            # set during those minutes must not look like ours.
            before = verify.snapshot(app)
            code = installer.install(path, kind, app.scope, plan, ctx.cancel)
            result = exitcodes.classify(code, kind)
            log.info("%s: installer exit code %s -> %s", app.name, code, result.kind)
            return result

        outcome = self._while_busy(attempt, progress, ctx)
        return Attempt(outcome, before, signer, sig_status, approved)

    def _winget(self, app, upd: Update, progress, ctx: UpdateContext) -> Attempt:
        ctx.cancel.check()
        running.close_for_update(ctx, app, progress)
        before = verify.Snapshot()

        def attempt() -> Outcome:
            nonlocal before
            progress("Preparing (winget)", -1)
            # winget downloads and installs in one process, so this is as close to the install as it gets.
            before = verify.snapshot(app)
            code, last = winget.update(
                upd.origin, upd.winget_mode,
                lambda pct: progress("Downloading (winget)", pct),
                lambda line: log.info("winget %s: %s", upd.origin, line),
                cancel=ctx.cancel,
                on_install=lambda: progress("Installing", -1),
            )
            return exitcodes.classify_winget(code, last)

        outcome = self._while_busy(attempt, progress, ctx)
        return Attempt(outcome, before)

    @staticmethod
    def _while_busy(attempt, progress, ctx: UpdateContext) -> Outcome:
        """Run `attempt()`, which returns an Outcome. While Windows reports another installation in progress
        (a Windows Update, say) wait and run it again; cancelling ends the wait at once."""
        outcome = attempt()
        for _ in range(BUSY_RETRIES):
            if not outcome.busy:
                break
            progress("Another installation is running, waiting…", -1)
            ctx.cancel.wait(BUSY_WAIT_SECONDS)
            ctx.cancel.check()
            outcome = attempt()
        return outcome
