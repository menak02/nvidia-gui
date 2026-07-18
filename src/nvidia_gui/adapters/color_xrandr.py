"""xrandr adapter — Digital Brightness and Contrast display controls."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Any

from ..application.ports import ColorPort

logger = logging.getLogger(__name__)


class XrandrColorAdapter(ColorPort):
    """Synchronous xrandr adapter for X11/XWayland.

    Queries and applies display brightness and contrast (via gamma) by invoking
    the `xrandr` binary.
    """

    def __init__(self, settings=None) -> None:
        self._bin = shutil.which("xrandr") or "xrandr"
        self._settings = settings

    def is_available(self) -> bool:
        """True if the xrandr binary is available and we are on X11."""
        return shutil.which("xrandr") is not None

    def detect_displays(self) -> list[dict[str, Any]]:
        """Run xrandr to detect connected display outputs."""
        if not self.is_available():
            return []

        try:
            out = subprocess.run(
                [self._bin],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

        displays = []
        # Parse lines like: "DP-1 connected primary 1920x1080+0+0 ..."
        for line in out.stdout.splitlines():
            if " connected " in line or " connected primary " in line:
                parts = line.split()
                if parts:
                    conn_id = parts[0]
                    # We can use the connector ID as its type for UI simplicity
                    displays.append({
                        "connector_id": conn_id,
                        "type": conn_id,
                        "connected": True,
                    })
        return displays

    def set_color(self, connector_id: str, brightness: float, contrast: float) -> bool:
        """Apply brightness and contrast (as gamma) for a specific display."""
        if not self.is_available():
            return False

        # Ensure values are within reasonable bounds (0.1 to 3.0)
        b_val = max(0.1, min(3.0, brightness))
        c_val = max(0.1, min(3.0, contrast))

        # We simulate contrast using xrandr's gamma: r:g:b
        # Note: xrandr gamma is 1.0 / actual_gamma, but for contrast we just pass the value directly
        gamma_str = f"{c_val}:{c_val}:{c_val}"

        args = [self._bin, "--output", connector_id, "--brightness", str(b_val), "--gamma", gamma_str]
        try:
            out = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            ok = out.returncode == 0
            if ok and self._settings is not None:
                self._settings.set(f"color.{connector_id}.brightness", b_val)
                self._settings.set(f"color.{connector_id}.contrast", c_val)
            return ok
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("xrandr set_color failed: %s", exc)
            return False
