# Upstream

Keeps the apps on a Windows PC up to date from their official sources. It reads every
installed app, finds the newest release from the project's own GitHub, then its official
site, and installs it with one click.

## Download

Get `Upstream-<version>-win-x64.zip` from the [Releases](https://github.com/siinsiw/upstream/releases)
page, unzip it anywhere (for example `C:\Users\<you>\Apps\Upstream`) and run `Upstream.exe`.
No Python needed. Windows 10 or 11, 64-bit. winget (App Installer) is recommended; it is built
into current Windows.

The build is not code-signed yet, so Windows SmartScreen may show "Windows protected your PC" the
first time: choose **More info → Run anyway**.

## Run from source

```bat
run.bat
```

The first run creates a Python environment in `%LOCALAPPDATA%\Upstream\venv` and installs
PySide6. Needs Python 3.11 or newer.

Build a standalone `Upstream.exe` (no Python needed on the target PC):

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1 -Dest C:\Users\<you>\Apps
```

## Where updates come from

For each app, in this order:

1. **The project's GitHub Releases.** The repository comes from a manual override, the
   links the app registered in Windows, or its winget manifest when the official installer
   is hosted on GitHub. Betas and release candidates are skipped unless you opt in. The
   Windows asset that matches the app's installer type and CPU architecture is chosen, and
   its SHA-256 is checked against the digest GitHub publishes.
2. **A site rule you define** (⋯ → Update source): page URL, a regex that finds the version,
   and an optional download URL template using `{version}`, `{version_nodots}`, `{major}`.
3. **The official site via winget.** winget manifests point at the vendor's own installer
   URL and pin its hash; winget downloads from there and verifies it.

If one source fails, the next is tried automatically.

## Installing

- The installer type is read from the file itself: MSI, MSIX, Inno Setup, NSIS, WiX Burn,
  InstallShield, Advanced Installer, Squirrel. These install silently.
- An unknown installer opens with its normal setup window; no guessed switches are sent.
- The Authenticode signature is checked and a tampered file is never run.
- Installers that need admin rights show the Windows UAC prompt.

## Uninstalling

Every app's ⋯ menu has **Uninstall…**. Upstream runs the app's own uninstaller, quietly when it
knows how (Windows Installer `/x`, the registered quiet command, Inno Setup, NSIS, winget for
Store apps) and with the vendor's normal window otherwise. It then waits for the uninstaller and
any helper it started, and checks that Windows no longer lists the app.

Some apps cannot be removed here: Upstream itself, the Python it runs on, winget/App Installer,
Windows frameworks, and entries marked as not removable. Shared runtimes and drivers get a warning.

Afterwards it can look for leftovers in files, folders, shortcuts, the registry, startup entries
and services. Each item is rated Certain, Likely, Possible or Protected, with the reason it was
found. Anything Windows or another installed app uses is Protected. The review is read-only for
now: nothing is deleted. Cleanup with backups and undo is the next step.

## Settings

- **Language:** English (default) or Persian (right-to-left). Switches instantly.
- **Theme:** System (follows Windows light/dark), Paper, Graphite, Midnight, Arctic, Dune,
  Moss, High contrast.
- **Sources:** use the GitHub CLI token when signed in; offer pre-releases.

## GitHub token

Without a token GitHub allows 60 requests an hour. If the GitHub CLI (`gh`) is signed in its
token is used, or set `GITHUB_TOKEN`. The token is only kept in memory. Responses are cached
with ETags, so repeat checks barely touch the quota.

## Files

| Path | Contents |
|---|---|
| `%APPDATA%\Upstream\settings.json` | language, theme, source overrides, ignored apps, skipped versions |
| `%LOCALAPPDATA%\Upstream\cache.json` | each app's repository and the latest releases |
| `%LOCALAPPDATA%\Upstream\downloads\` | installers; deleted after a successful install |
| `%LOCALAPPDATA%\Upstream\upstream.log` | full log |

Settings from the earlier name (Tazeh) are copied over on first start.

## Code

```
upstream/
  inventory.py   installed apps from the Uninstall registry keys
  winget.py      winget list / show / upgrade
  github.py      repository discovery, releases, installer asset choice
  site.py        user-defined official-site rule
  scanner.py     decides each app's source
  installer.py   download, hash and signature checks, silent install
  updater.py     runs one update with fallbacks
  uninstaller.py plans and runs an app's own uninstaller, then verifies it is gone
  leftovers.py   read-only leftover scan with confidence levels
  guard.py       paths and registry keys that are never offered for removal
  i18n.py        English source strings and the Persian table
  ui/            window, rows, settings, uninstall dialog, themes (PySide6)
```

## License

MIT, see `LICENSE`. The Windows build bundles Qt for Python (LGPL-3.0) and other components
listed in `THIRD-PARTY-NOTICES.md`.
