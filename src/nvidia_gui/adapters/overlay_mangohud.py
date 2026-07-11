"""MangoHud overlay helper — ensures a sensible default config exists.

The per-game toggle lives in the profile (``enable_mangohud`` -> ``MANGOHUD=1``).
This helper just writes a reasonable MangoHud.conf once if the user has none,
so enabling the overlay actually shows something useful out of the box. It
never overwrites an existing config.
"""

from __future__ import annotations

import logging
import pathlib

logger = logging.getLogger(__name__)

_DEFAULT_CONF = """\
# Managed by nvidia-gui (only written if you had no config). Edit freely.
# Compact GPU overlay matching the NVIDIA-GUI dashboard readout.
gpu_stats
cpu_stats
ram
fps
frametime
frame_timing
hud_no_shimmer
font_size=14
background_alpha=0.6
position=top-left
"""


def ensure_default(overwrite: bool = False) -> pathlib.Path | None:
    path = pathlib.Path.home() / ".config" / "MangoHud" / "MangoHud.conf"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("MangoHud config dir unwritable: %s", exc)
        return None
    if path.is_file() and not overwrite:
        return path
    try:
        path.write_text(_DEFAULT_CONF, encoding="utf-8")
    except OSError as exc:
        logger.warning("MangoHud config write failed: %s", exc)
        return None
    return path


def is_available() -> bool:
    import shutil as _s
    return _s.which("mangohud") is not None
