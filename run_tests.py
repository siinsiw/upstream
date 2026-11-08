"""Run the whole unit suite in one process and report a trustworthy exit code.

    set QT_QPA_PLATFORM=offscreen
    python -X utf8 run_tests.py [pattern]

A plain `python -m unittest discover` can die with an access violation while Windows unloads Qt when UI tests left
widgets alive, and the exit code is then useless. This ends the process with TerminateProcess after the results are
printed, which skips that teardown."""
from __future__ import annotations

import ctypes
import sys
import unittest
from ctypes import wintypes


def main() -> int:
    pattern = sys.argv[1] if len(sys.argv) > 1 else "test*.py"
    suite = unittest.defaultTestLoader.discover("upstream/tests", pattern=pattern, top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.stdout.flush()
    sys.stderr.flush()
    return 0 if result.wasSuccessful() else 1


def terminate(code: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess(kernel32.GetCurrentProcess(), code)


if __name__ == "__main__":
    terminate(main())
