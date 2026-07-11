#!/usr/bin/env python3
"""Entry shim — delegates to :mod:`nvidia_gui.presentation.app`.

Kept at the project's historical path (``src/main.py``) so existing shell
aliases and the desktop entry continue to launch the app. The real GTK4 app
lives in :mod:`nvidia_gui.presentation.app`.

Run directly:  ``python3 src/main.py``
Console entry point:  ``nvidia-gui``  (pyproject: ``main:main``)
"""
from __future__ import annotations

import pathlib
import sys

# Allow running directly with no install: make ``nvidia_gui`` importable.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from nvidia_gui.presentation.app import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
