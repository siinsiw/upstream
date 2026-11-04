# Third-party notices

Upstream's own code is under the MIT license (see `LICENSE`). The Windows build bundles the
components below, each under its own license.

## Qt for Python (PySide6, Shiboken6) and Qt 6

- License: GNU Lesser General Public License v3.0 (LGPL-3.0)
- Project: https://www.qt.io/qt-for-python
- Source: https://code.qt.io/cgit/pyside/pyside-setup.git and https://download.qt.io/official_releases/
- License text: https://www.gnu.org/licenses/lgpl-3.0.html

The build is a one-folder PyInstaller bundle: the Qt and PySide6 libraries sit as separate,
unmodified files in `_internal\PySide6`, so they can be replaced with other compatible builds.

## Python

- License: Python Software Foundation License Version 2
- Project: https://www.python.org/
- License text: https://docs.python.org/3/license.html

## PyInstaller bootloader

- License: GPL-2.0 with the PyInstaller bootloader exception, which allows distributing the
  bundled application under its own terms
- Project: https://pyinstaller.org/
- License text: https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt
