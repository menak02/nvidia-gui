"""nvibrant adapter — Digital Vibrance display controls."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Any

from ..application.ports import VibrancePort

logger = logging.getLogger(__name__)

# Pattern for parsing nvibrant output line-by-line:
# • (0, HDMI) • Set vibrance (  512) • Success
# or • (2, DP  ) • Set vibrance (    0) • None
_LINE_PATTERN = re.compile(
    r"•\s*\((\d+),\s*([A-Za-z0-9]+)\s*\)\s*•\s*Set\s+vibrance\s*\(\s*(-?\d+)\)\s*•\s*(\w+)"
)


class NvibrantVibrance(VibrancePort):
    """Synchronous nvibrant adapter.

    Queries and applies display vibrance settings by invoking the `nvibrant`
    binary on system PATH.
    """

    def __init__(self, settings=None) -> None:
        self._bin = shutil.which("nvibrant") or "nvibrant"
        self._settings = settings

    def is_available(self) -> bool:
        """True if the nvibrant binary/command is available on PATH."""
        return shutil.which("nvibrant") is not None

    def detect_displays(self) -> list[dict[str, Any]]:
        """Run nvibrant to detect display connectors and return their attributes.

        If we have saved values from settings, we run with them to avoid resetting
        user configurations during detection.
        """
        if not self.is_available():
            logger.debug("nvibrant binary not available on PATH")
            return []

        # Load current values from settings to avoid resetting them
        current_values = []
        if self._settings is not None:
            current_values = self._settings.get("vibrance.values", [])

        args = [self._bin] + [str(v) for v in current_values]
        try:
            out = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("nvibrant execution failed: %s", exc)
            return []

        # Parse output
        displays = []
        for line in out.stdout.splitlines():
            match = _LINE_PATTERN.search(line)
            if match:
                conn_id = int(match.group(1))
                conn_type = match.group(2).strip()
                status = match.group(4).strip()
                connected = status.lower() != "none"
                displays.append({
                    "connector_id": conn_id,
                    "type": conn_type,
                    "connected": connected,
                })
        return displays

    def set_vibrance(self, values: list[int]) -> bool:
        """Apply vibrance values for all physical display outputs.

        The list length must match the number of connectors reported.
        Returns True on success, False on failure.
        """
        if not self.is_available():
            logger.debug("nvibrant binary not available on PATH")
            return False

        args = [self._bin] + [str(v) for v in values]
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
                self._settings.set("vibrance.values", values)
            return ok
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("nvibrant set_vibrance failed: %s", exc)
            return False
