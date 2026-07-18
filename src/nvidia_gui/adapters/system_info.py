"""Read-only driver + display info."""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
import subprocess

from ..application.ports import DisplayInfoPort, DriverInfoPort
from ..domain.models import DisplayInfo, DriverInfo

logger = logging.getLogger(__name__)


class SmiDriverInfo(DriverInfoPort):
    def read(self) -> DriverInfo:
        # DriverInfo is frozen; collect then construct once at the end.
        driver_version = ""
        module_version = ""
        branch = ""
        # driver version + module version from /proc/driver/nvidia/version
        try:
            text = pathlib.Path("/proc/driver/nvidia/version").read_text(errors="replace")
            for tok in text.split():
                if tok and tok[0].isdigit() and "." in tok:
                    driver_version = tok
                    break
            branch = "open" if "open kernel" in text.lower() else "proprietary"
        except OSError:
            pass
        # module version fallback via modinfo (also fills module_version)
        if not driver_version:
            mi = shutil.which("modinfo")
            if mi:
                try:
                    r = subprocess.run([mi, "nvidia"], capture_output=True, text=True, timeout=4)
                    for line in r.stdout.splitlines():
                        if line.startswith("version:"):
                            v = line.split(":", 1)[1].strip()
                            if not driver_version:
                                driver_version = v
                            module_version = v
                except (subprocess.TimeoutExpired, OSError):
                    pass
        return DriverInfo(
            driver_version=driver_version,
            module_version=module_version,
            branch=branch,
            module_name="nvidia",
            modprobe_config_path="/etc/modprobe.d/nvidia-gui.conf",
            compositor_incompatible=True,  # nvidia-settings writes are inert here
        )


class EnvDisplayInfo(DisplayInfoPort):
    def read(self) -> DisplayInfo:
        server = "Wayland" if os.environ.get("WAYLAND_DISPLAY") else (
            "X11" if os.environ.get("DISPLAY") else "Unknown"
        )
        monitors = _count_monitors(server)
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
        vrr = _vrr_capable()
        note = (
            "Digital vibrance and color (brightness & contrast) controls are now active. "
            "Native hardware mode setting remains read-only on Wayland."
        )
        return DisplayInfo(
            server=server,
            monitors=monitors,
            vrr_capable=vrr,
            gsync_capable=vrr,  # equivalent capability surface for our purposes
            notes=f"{desktop or 'Unknown desktop'} — {note}",
        )


def _vrr_capable() -> bool:
    """Best-effort VRR/FreeSync/GSYNC capability probe.

    True if any DRM connector advertises a VRR-capable property (the standard
    drm property is named ``vrr_capable``). Read-only; never claims capability
    we can't evidence.
    """
    sysdrm = pathlib.Path("/sys/class/drm")
    if not sysdrm.is_dir():
        return False
    # /sys/class/drm/card*-* connectors don't surface a `vrr_capable` file at
    # the class-device level (it lives under device/drm in sysfs); xrandr
    # --props is the robust read on an X11 or Wayland session below.
    if shutil.which("xrandr"):
        try:
            r = subprocess.run(
                ["xrandr", "--props"], capture_output=True, text=True, timeout=4
            )
            return "vrr_capable" in r.stdout or "Variable" in r.stdout
        except (subprocess.TimeoutExpired, OSError):
            pass
    return False


def _count_monitors(server: str) -> tuple[str, ...]:
    # Wayland (wlroots): wlr-randr. X11: xrandr. Both may be absent.
    if server == "X11":
        xr = shutil.which("xrandr")
        if xr:
            try:
                r = subprocess.run([xr], capture_output=True, text=True, timeout=4)
                return tuple(
                    ln.split()[0] for ln in r.stdout.splitlines()
                    if ln and " connected" in ln
                )
            except (subprocess.TimeoutExpired, OSError):
                pass
    if server == "Wayland":
        wr = shutil.which("wlr-randr")
        if wr:
            try:
                r = subprocess.run([wr], capture_output=True, text=True, timeout=4)
                # wlr-randr prints monitor names as first non-indented token lines
                return tuple(
                    ln.split()[0] for ln in r.stdout.splitlines()
                    if ln and not ln.startswith(" ") and not ln.startswith("\t")
                )
            except (subprocess.TimeoutExpired, OSError):
                pass
    return ()
