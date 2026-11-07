import sys

from upstream import stability

stability.install_crash_handlers()  # first, so a failing import below is reported too

from upstream.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
