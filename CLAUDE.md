# Upstream: working notes for Claude (and any contributor)

Upstream is a Windows desktop app (Python 3.12 + PySide6, MIT) that keeps installed apps up to date from their
official sources and uninstalls them completely. It reads every installed app, finds the newest release (the
project's own GitHub Releases first, then a site rule the user defined, then winget, whose manifests point at the
vendor's own installer), and installs it with one click. It also has a full uninstaller with a reversible leftover
cleanup. UI: English by default, Persian (right-to-left) selectable at run time, eight themes.

- Version: `upstream/__init__.py` (`__version__`). Repository: github.com/siinsiw/upstream (public, MIT).
- Platform: Windows 10 or 11, 64-bit. winget is recommended (it ships with current Windows). Needs nothing else.
- Chat language: the maintainer writes Persian, so reply in Persian in conversation. Code, comments, commit
  messages, docs and every string key are English.

## Commands (run from the repository root)

| Task | Command |
|---|---|
| Run from source | `run.bat` (creates `%LOCALAPPDATA%\Upstream\venv` on first run; the venv must stay in a short path, PySide6 DLLs fail to load from very long folders) |
| All tests, one process | `set QT_QPA_PLATFORM=offscreen` then `python -X utf8 run_tests.py` (about 1,550 tests, 12 to 16 minutes) |
| One test module | `python -X utf8 run_tests.py test_cleanup.py` (same env; takes a glob pattern) |
| Translation gate | `python -X utf8 tools/check_i18n.py` (must print 0 missing and 0 placeholder mismatches; exit code 1 on any problem) |
| Build the exe | `powershell -ExecutionPolicy Bypass -File build.ps1 -Dest <folder>` gives `<folder>\Upstream\Upstream.exe` (PyInstaller, one folder, no console) |

Use the venv interpreter (`%LOCALAPPDATA%\Upstream\venv\Scripts\python.exe`) and always `-X utf8`.
Tests are stdlib `unittest` (no pytest, no other dependency; the only runtime dependency is PySide6).

## Source priority and the update flow

1. Scan: `inventory.py` reads the three Uninstall registry hives (HKLM 64, HKLM 32, HKCU); `winget.py` adds winget
   rows (Store/MSIX apps have no registry entry) and each row's manifest (`winget show`); `scanner.py` decides, per app,
   the update source in this order: GitHub (`github.py`: repo from a manual override, links the app registered, or the
   winget manifest; releases with ETag cache; `pick_asset` chooses the Windows asset by installer family and arch),
   a user site rule (`site.py`), winget. Failure of one source falls through to the next (`AppStatus.update` + `fallbacks`).
2. Pins (`pins.py`) hold an app on a version or a major line. Badges (`badges.py`) mark security notes, end of life
   (endoflife.date, cached) and "very outdated". Download sizes (`Update.size`) come from the GitHub asset or an HTTP
   HEAD on the winget installer URL (budgeted, cached per manifest version). The result is saved to `last_scan.json`
   so the next start shows it at once.
3. Update: `updater.Updater.run(status, progress, ctx)` runs the chosen source, then fallbacks, through fixed steps,
   each its own module: `net.preflight` (metered/battery policy) -> `net.download` (proxy-aware, resumable, https only
   unless a checksum is published) -> SHA-256 and Authenticode check (`installer.signature`) -> `trust.evaluate`
   (publisher gate) -> `installer.detect` (installer family from bytes) -> `inplace.plan` (replace, never a second
   copy) -> `running.close_for_update` -> `installer.install` (ShellExecuteEx, cancellable wait) -> `exitcodes.classify`
   -> `verify.check` (what Windows lists now: installed, restart, side by side, unchanged, unknown). winget updates go
   through the same busy-retry, cancel and verify code. Messages are always `(English template, values)` pairs.
4. Uninstall: `uninstaller.py` plans and runs the app's own uninstaller (MSI `/x`, quiet command, Inno, NSIS, winget for
   Store apps) and checks that Windows no longer lists it. `leftovers.py` (read-only scan, confidence levels Certain,
   Likely, Possible, Protected, each with a reason) and `guard.py` (paths and registry keys that are never offered)
   feed `cleanup.py`, which backs up, removes and can undo. `ui/uninstall.py` holds the single, batch and backups dialogs.

## Module map (`upstream/`)

Core: `app.py` (start-up order, launch arguments `--minimized`, `--elevated`, `--wait-pid`), `config.py` (Settings, Cache,
JSON stores, log handler), `context.py` (`CancelToken`, `Question`, `UpdateContext(settings, cancel, ask_fn, bulk, auto)`),
`errors.py` (`InstallError(template, **values)`, `Cancelled`, `StoppedWaiting`), `i18n.py`, `translations/`, `versioning.py`.
Detection: `inventory.py`, `winget.py`, `github.py`, `site.py`, `scanner.py`, `pins.py`, `badges.py`, `discover.py`.
Install pipeline: `net.py`, `trust.py`, `installer.py`, `inplace.py`, `running.py`, `exitcodes.py`, `verify.py`, `updater.py`.
Uninstall: `uninstaller.py`, `leftovers.py`, `guard.py`, `cleanup.py`.
Features: `history.py` (JSON Lines, rotated), `background.py` (tray, schedule, notifications, per-app automatic updates),
`details.py` (facts, Repair, Modify), `export.py` (json, csv, winget import), `elevation.py` (restart once as administrator),
`stability.py` (single instance, crash reports, housekeeping, `exit_now`).
UI (`ui/`): `window.py` (main window, install queue), `widgets.py` (lazy `AppRow`, source dialog), `library.py` (All apps tab),
`settings.py`, `details.py`, `history.py`, `discover.py`, `uninstall.py`, `tray.py`, `crash.py`, `theme.py`, `locale.py`.
`tools/check_i18n.py` is the translation gate. `main.py` installs crash handlers first, then runs `app.main()` and leaves
through `stability.exit_now`.

## Threads, state and data

- The UI thread owns every widget. Workers (scan, one update at a time, searches, details lookups, housekeeping) talk to
  the UI only through Qt signals on a `Bus`/`_Bus` QObject; never touch a widget from a worker.
- Updates run one at a time on a single job thread (`MainWindow._job_loop` / `_run_job`). Each job has a `CancelToken`;
  after a cancel the queue waits `WIND_DOWN` (2 s) and moves on, so a step that ignores the token (winget) cannot block it.
  A cancel never kills an installer that has started (it may be mid-write): the wait ends and `StoppedWaiting` is raised.
  A scan never overlaps jobs (`request_update` refuses while scanning, `start_scan` refuses while jobs run).
- `UpdateContext.ask()` blocks the worker until the user answers in a dialog; `ctx.auto` (automatic updates started by
  `background.py`) makes it return No at once, so an unattended update never asks, never opens a setup window
  (`updater` refuses installers with no silent mode) and never closes an app.
- `MainWindow(settings, cache, *, persist=False)`: history and snapshot are written only with `persist=True` (app.py);
  tests build windows with the default and never touch the data folder.
- Start-up order in `app.main()`: parse launch arguments, logging, `QApplication`; single instance (a named mutex per
  user and session is the lock, a user-only `QLocalServer` only hands the window over; `--wait-pid` waits for the process
  that relaunched us instead); `migrate_legacy`, `Settings` and `_clean_settings` (every stored value is validated),
  `configure` of net, installer, history and cleanup; language and theme; `MainWindow(persist=True)`; `show_last_scan()`;
  `Background.start()`; show the window; then timers: the first scan (150 ms), the crash-report dialog if the last run
  crashed, housekeeping (a daemon thread) and the old-backup purge. Exit goes through `stability.exit_now` (flushes the
  clipboard and logs, then `os._exit`) because daemon threads inside Qt can abort a normal interpreter shutdown.

Files (`CONFIG_DIR` = `%APPDATA%\Upstream`, `DATA_DIR` = `%LOCALAPPDATA%\Upstream`):

| Path | What |
|---|---|
| `CONFIG_DIR\settings.json` | all user settings (see `config.Settings` defaults; also written with defaults at use: `last_check_ts`, `notified`, `auto_tried`, `updates_sort`) |
| `DATA_DIR\cache.json` | winget manifests (repo links, installer URL and type, size), GitHub releases with ETags, endoflife tables |
| `DATA_DIR\last_scan.json` | the last scan (no tokens), for the instant start |
| `DATA_DIR\history.jsonl` (+ `history.1.jsonl`) | the install, update and uninstall record, local only |
| `DATA_DIR\quarantine\<id>\` | one leftover-cleanup backup: `manifest.json`, moved files, `.reg` exports |
| `DATA_DIR\downloads\` | installers and resumable `.part` files with a `.part.json` sidecar |
| `DATA_DIR\crashes\` | offline crash reports and per-process `fault-<pid>.log` files |
| `DATA_DIR\upstream.log` | rotating log (about 1 MB x 3) |
| `DATA_DIR\theme\` | the drawn check and chevron glyphs the stylesheet refers to |

Settings from the old name (Tazeh) are copied once by `config.migrate_legacy`. Writes to the registry happen only for
explicit user actions: the HKCU Run value `Upstream` (start with Windows) and a cleanup's Undo (`reg import`).

## Safety rules that must keep holding

- Nothing is installed unless its SHA-256 matches (when published) and its Authenticode signature is not broken; the
  publisher gate (`trust.py`) asks or blocks when the signer differs from the app's usual publisher (strong baselines need
  an exact normalised match; unattended updates without a checksum need a Valid signature matching the baseline).
- Downloads are https only unless a checksum is published (loopback exempt for tests); redirects never go from https to
  http or to another scheme; size lookups use HEAD and never start a download.
- One installed copy: `inplace.plan` refuses an installer family or MSI upgrade code that would leave a second copy.
- `guard.py` lists what is never offered for removal; `cleanup.py` re-checks it right before each removal, refuses
  links and junctions, other installed apps' folders, anything that changed since the scan, and a failed read of the
  installed apps (fail closed). Files are moved into a backup, never deleted outright; `.reg` imports are confined to the
  backed-up key; restore never overwrites. Nothing self-elevates; admin-only items fail with a clear message.
- Closing an app: only processes whose image lies inside the app's own install folder, after re-checking pid, path and
  start time; graceful `WM_CLOSE` first, a forced stop only after a separate question, never in unattended mode.
- No persistent elevated helper (a process reachable by other programs would be an escalation hole): the supported way to
  get one UAC prompt for a batch is to restart Upstream itself as administrator (`elevation.py`).
- Nothing is sent anywhere except requests to GitHub, winget's sources, the sites a user rule names, endoflife.date and the
  vendors' own installer URLs (the download, and a HEAD for its size).
  Crash reports stay on disk. No telemetry. Tokens (`GITHUB_TOKEN`, `gh auth token`) are kept in memory only.
- The install pipeline calls PowerShell and msiexec by full path under `%SystemRoot%`, and file paths reach PowerShell through an
  environment variable, never spliced into the command text (curly quotes would otherwise break out of the quoting).

## i18n: English is the key, Persian is a table

- Every user-visible string is an English template; `tr("Installed {version}", version=v)` looks the template up in the
  Persian table and fills the values afterwards. `trn(singular, plural, n)` for counts. `num()` gives Persian digits in
  Persian, `isolate()` wraps Latin values (versions, names, paths) so they stay intact in right-to-left text.
- The Persian table is `i18n.FA` (older strings) merged with one module per feature in `translations/` (`PARTS` in
  `translations/__init__.py`; a new feature adds a part there). `import upstream.i18n` raises on a key defined by two
  parts, and `tools/check_i18n.py` also reports a key defined in both `i18n.py` and a part. A part wins silently over the
  base otherwise, so never duplicate.
- Messages that travel (errors, progress, notes, outcomes, verdicts, badges, questions) are `(template, values)` pairs and
  are rendered at display time with `render(template, values)`, so a language switch re-renders them. Never pass an
  f-string to `tr`, `InstallError`, `Cancelled`, `Outcome`, `Verdict`, `Decision`, `ctx.ask`, `progress` or `set_note`.
  A template stored in a dataclass, tuple or table is wrapped in `N_("...")` (marks it for the checker). `english()` renders
  the same pair for the log file. `render` converts stored arguments named `source` and `error` through `tr`, and `percent`,
  `minutes`, `n` through `num()`.
- The checker (`tools/check_i18n.py`) collects templates from those calls, requires a Persian entry with the same
  `{placeholders}`, forbids Persian text outside `i18n.py` and `translations/` (so also in tests: use `i18n.FA[...]` or
  escapes), and warns about the style rules below.
- Persian style: informal "تو" register (no «کنید»); ZWNJ (نیم‌فاصله) in «می‌», «نمی‌», plural «ها», «تر/ترین» and compounds
  such as «دانلود‌شده»; Persian ک and ی only, never Arabic ك ي; no Arabic-Indic digits; do not start a string with a Latin word
  (a label then lays the whole sentence out left to right; rewrite so a Persian word comes first). Glossary decisions:
  delete = «پاک», remove = «حذف», pin = «سنجاق», rollback = «بازگشت به نسخهٔ قبلی», administrator = «مدیر», publisher = «ناشر»,
  installer = «نصب‌کننده», backup = «پشتیبان», restore = «بازگردانی».

## UI conventions

- Colours come from the theme tokens (`ui/theme.py`, `theme.current()`); never hard-code one. Eight themes: system, paper,
  graphite, midnight, arctic, dune, moss, contrast. Contrast of text and chips is at least 4.5:1 in all of them and a test checks it.
- Language and theme switch live. A widget with text implements `retranslate()`; rows are rebuilt by `populate()` and the
  window replays each job's last state (`job_state`). Do not cache translated text.
- RTL: use `i18n.is_rtl()`, `ltr_label` for versions and paths, mirrored layouts (never hard-code left or right), and look at a
  Persian screenshot of anything you build.
- Rows are lazy: `AppRow` builds its children on first show, size hint or job state. Use `shiboken6.isValid(row)` before touching
  a row you kept (`MainWindow._live_rows`). `LibraryPage` places rows by hand and builds only those near the viewport; the
  Updates tab does the same with one measured row height. A long note must elide (one line, full text in the tooltip).
- Dialogs that start worker threads emit through a `_Bus` QObject and must tolerate being closed first (stop token, no
  work for a deleted dialog). Every control is reachable by keyboard and has a visible focus ring (`theme.py`). The window
  minimum is 800x520 and the header must not clip in English or Persian (`_fit_header`).

## Testing

- One process, offscreen: `QT_QPA_PLATFORM=offscreen`, `python -X utf8 run_tests.py`. The runner ends with `TerminateProcess`
  because unloading Qt with widgets still alive can crash the interpreter at exit (and the exit code would be useless).
  Layout tests need real font metrics: set `QT_QPA_FONTDIR=C:/Windows/Fonts` for them (they skip without it) and for
  screenshots, but do not export it for the full run (one settings test then fails). Never set it inside a test file.
- Tests never run a real installer, uninstaller, `winget install/upgrade`, elevation or tray, never write the real
  `%APPDATA%\Upstream` / `%LOCALAPPDATA%\Upstream` (patch the folders to a temp dir; `MainWindow` defaults to `persist=False`),
  never touch other processes (a test may start and close a throwaway process from a temp folder), and use network only
  through a local `http.server` on 127.0.0.1. The only registry writes allowed are throwaway keys or values named
  `UpstreamTest_<random>` removed in a `finally` (cleanup, autostart tests).
- Style of tests: wait for the final condition, not an earlier one (`pump(lambda: label.text() == "Removing Foo")`, not "the
  worker was called"): the earlier form was a real race. Windows paths in test code are raw strings (a plain `"\X"` is an
  invalid escape). No Persian literals (the checker flags them).
- A change is done when its module's tests pass, `tools/check_i18n.py` is clean, and for anything visible a screenshot in
  English and Persian looks right. When a fix is made for a reproduced bug, the test must fail without the fix.

## Code style and git

- Match the surrounding code: terse, English comments only for the non-obvious why, `from __future__ import annotations`, type
  hints, no dead code or TODOs, no new dependency (stdlib, ctypes and PySide6 only; the exe is built with PyInstaller, so imports
  must be static: a new translation part is listed in `translations/__init__.py`, not discovered).
- Never guess an installer's silent switches: the framework is read from the file's bytes (`installer.detect`); an unknown
  one opens its normal setup window. Exit codes are mapped only where the documentation is certain (`exitcodes.py` cites it).
- Keep each file's existing line endings (the working copy is mostly CRLF, `core.autocrlf=true`; LF to CRLF warnings are
  harmless). Set `git config core.longpaths true` on machines with long paths.
- `main` is the released branch. Commit messages: a short imperative summary, a body that says why. Do not commit
  profiler output (`*.out`, `*.prof` are ignored), logs, screenshots that show a person's installed apps, or anything with a
  user name in a path.

## Release checklist

1. Bump `__version__` in `upstream/__init__.py`; update `README.md` (features, files table, code map).
2. Full test run (`run_tests.py`) and `tools/check_i18n.py` clean.
3. Build into a staging folder (`build.ps1 -Dest <folder>`). Smoke test the exe with `LOCALAPPDATA` and `APPDATA` pointing to a
   temp folder: it must start, show a window, and exit with code 0 and no file in `crashes\` when its window is closed normally.
   (Killing it leaves a `fault-<pid>.log` that looks like a crash: close it normally.) Check every `upstream.*` module is inside
   the exe (PyInstaller's archive reader lists the pyz).
4. Install by moving folders, not mirroring: stop the running app, move the old install folder aside, move the new one into
   place (shortcuts point to `<Apps>\Upstream\Upstream.exe`), recycle the old folder.
5. Zip the folder as `Upstream-<version>-win-x64.zip`, record its SHA-256, tag `v<version>` and publish a GitHub release with
   the zip. Publishing is outward-facing: do it only when the maintainer says so.
6. The exe is not code-signed: Windows SmartScreen may say "Windows protected your PC" (More info, then Run anyway). The README says so.

## Working method for big changes (what worked here)

1. Write a plan: architecture, the fixed public signatures between modules, and per-agent file ownership with disjoint files.
   First do the shared infrastructure alone (stub modules with final signatures, settings defaults, translation parts, window
   API), then let parallel agents build features, each in its own files with its tests and its translation part.
2. Wire features into the shared UI files (`window.py`, `widgets.py`, `settings.py`, `app.py`) with agents that own those files,
   so two agents never edit one file at once.
3. Review in parallel with read-only reviewers (pipeline correctness, safety and security, UI with screenshots, backend and
   unattended behaviour, Persian and English text), reproduce each finding with a snippet before fixing, fix by file ownership,
   then do one last wording pass (wording touches many files), then the full suite, the build and the smoke test.
4. A fix is accepted only with a test that fails without it.

## Environment pitfalls

- Shell tools may halve a double backslash in text you write through them: use raw strings or `chr(92)` for Windows paths in
  generated code, and check the result. A very large heredoc can fail to parse: write in smaller pieces.
- A permission layer may block `robocopy /MIR` (it can delete): swap folders by moving them instead.
- Processes started from a sandboxed tool may see a package-local copy of `%LOCALAPPDATA%`; anything the user must see belongs
  outside AppData. A venv in a very long path breaks PySide6 DLL loading.

## Not built yet, and known limits

- Force-removing apps that have no uninstaller (Steam, NoxPlayer), a startup-apps tab, a command-line interface and portable
  build, self-update with a real installer and code signing, a keyboard and screen-reader audit, and install tests inside
  Windows Sandbox. Nothing has been run for real on a real app yet (installs, uninstalls and cleanups are tested with fakes
  and throwaway keys): try the first real run on an unimportant app.
- A proxy password is stored in plain text in `settings.json`. A removed Windows service cannot be restored by Undo (reinstall
  the app). winget downgrades are best effort. Some servers give no download size, so the row shows none. Badges map only
  about twenty products to end-of-life data, on purpose.

## License

MIT (`LICENSE`). The Windows build bundles Qt for Python (LGPL-3.0), Python and the PyInstaller bootloader: see
`THIRD-PARTY-NOTICES.md`.
