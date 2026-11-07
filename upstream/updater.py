"""Runs one update: the chosen source first, then the fallbacks in order.

Each source goes through the same steps, and each step lives in its own module:

    net.preflight        metered connection / battery policy
    net.download         resumable, proxy-aware download            (direct sources)
    trust.evaluate       is the signer the app's usual publisher?
    installer.detect     which installer framework the file is
    inplace.plan         switches that make it replace the installed copy
    running.close_...    close the app's open processes
    installer.install    run it, waiting without blocking the queue
    exitcodes.classify   what the exit code means
    verify.check         what really changed on the PC

What a source's outcome does to the run:

    ok, restart          verify.check decides. Installed, restart, side by side and unknown end the run as done;
                         unchanged ends it as a failure that keeps the update offered, without trying another
                         source (a setup window the user closed also exits 0)
    cancelled            ends the run; no other source starts
    busy                 the same installer is run again a few times while Windows finishes the other installation
                         (the cancel token ends the wait), then the run ends: another source would meet the same lock
    already_installed,   done only if verify.check finds the new version listed; otherwise the next source is tried
    newer_present
    disk_full            ends the run; no other source has more room
    needs_admin, failed  the next source is tried, and the last error is what the row shows

Messages travel as (English template, values) pairs and are translated by the
UI when shown, so switching language mid-install re-renders them correctly."""
from __future__ import annotations

import logging

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
            signer = ""
            before = verify.snapshot(app)
            try:
                ctx.cancel.check()
                if upd.source == "winget":
                    outcome = self._winget(app, upd, progress, ctx)
                else:
                    outcome, signer = self._direct(app, upd, progress, ctx)
            except installer.Cancelled as e:
                return False, e.template, e.values, app.version
            except installer.InstallError as e:
                outcome = Outcome("failed", e.template, e.values)
            except Exception as e:
                log.exception("update of %s via %s failed", app.name, upd.source)
                outcome = Outcome("failed", "{error}", {"error": str(e)})

            if outcome.cancelled:
                # The user stopped it (closed the installer, declined UAC): do not start another source.
                return False, outcome.template or "Cancelled.", outcome.values, app.version
            if outcome.ok or outcome.present:
                verdict = verify.check(app, before, outcome, upd.version)
                log.info("%s: %s (%s)", app.name, verdict.kind, english(verdict.template, verdict.values))
                if outcome.ok or verdict.kind in _APPLIED:
                    if signer and verdict.kind in ("installed", "restart"):
                        trust.remember(ctx.settings, app, signer)
                    return verdict.ok, verdict.template, verdict.values, verdict.version
            error = (outcome.template, outcome.values)
            log.warning("%s: %s failed: %s", app.name, upd.source, english(*error))
            if outcome.kind in _NO_FALLBACK:
                break
            if index + 1 < len(attempts):
                progress("{source} failed, trying the next source…", -1,
                         source=SOURCE_LABELS.get(upd.source, upd.source))
        return False, error[0], error[1], app.version

    def _direct(self, app, upd: Update, progress, ctx: UpdateContext) -> tuple[Outcome, str]:
        progress("Downloading", 0)

        def on_bytes(done, total):
            progress("Downloading", int(done * 100 / total) if total else -1)

        path = net.download(upd.download_url, upd.file_name, on_bytes, upd.sha256, cancel=ctx.cancel)
        log.info("%s: downloaded %s (sha256 %s)", app.name, path.name,
                 "verified" if upd.sha256 else "not published")

        progress("Checking signature", -1)
        sig_status, signer = installer.signature(path)
        log.info("%s: Authenticode %s %s", app.name, sig_status, signer)
        if sig_status == "HashMismatch":
            path.unlink(missing_ok=True)
            raise installer.InstallError("The file's digital signature is broken (tampered); nothing was installed.")

        decision = trust.evaluate(ctx.settings, app, sig_status, signer)
        if decision.action == "block":
            path.unlink(missing_ok=True)
            raise installer.InstallError(decision.template, **decision.values)
        if decision.action == "ask" and not ctx.ask(decision.template, detail=decision.detail, danger=True,
                                                    yes="Install anyway", no="Don't install", **decision.values):
            path.unlink(missing_ok=True)
            raise installer.Cancelled("Not installed: you declined a file from a different publisher.")

        kind = installer.detect(path, upd.installer_type)
        log.info("%s: installer type %s", app.name, KIND_LABELS.get(kind, kind))
        plan = inplace.plan(app, kind, path)
        for template, values in plan.warnings:
            log.warning("%s: %s", app.name, english(template, values))
        if plan.refuse:
            path.unlink(missing_ok=True)
            raise installer.InstallError(plan.refuse[0], **plan.refuse[1])

        running.close_for_update(ctx, app, progress)

        def attempt() -> Outcome:
            progress("Installing" if kind != "exe" else "Setup window opened", -1)
            code = installer.install(path, kind, app.scope, plan, ctx.cancel)
            result = exitcodes.classify(code, kind)
            log.info("%s: installer exit code %s -> %s", app.name, code, result.kind)
            return result

        outcome = self._while_busy(attempt, progress, ctx)
        if outcome.ok:
            path.unlink(missing_ok=True)
        # Only a verified signature may become the app's remembered publisher.
        return outcome, (signer if sig_status == "Valid" else "")

    def _winget(self, app, upd: Update, progress, ctx: UpdateContext) -> Outcome:
        running.close_for_update(ctx, app, progress)

        def attempt() -> Outcome:
            progress("Preparing (winget)", -1)
            code, last = winget.update(
                upd.origin, upd.winget_mode,
                lambda pct: progress("Downloading (winget)", pct),
                lambda line: log.info("winget %s: %s", upd.origin, line),
            )
            return exitcodes.classify_winget(code, last)

        return self._while_busy(attempt, progress, ctx)

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
