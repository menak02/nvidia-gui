"""Original SVG line-icon loader — emoji-free navigation iconography.

Replaces every emoji in the nav rail / overlay menu / section headers with a
real monochrome, ``currentColor`` SVG so the app reads as a professional
NVIDIA-App neighbour rather than a terminal-emoji mock. Every icon is an
ORIGINAL line drawing (no NVIDIA marks, no Nerd-Font/third-party glyph), MIT.

``icon(name)`` resolves to ``icons/<name>.svg`` when shipped and falls back to
``icons/_placeholder.svg`` so the rail is never blank before the design-keeper
agent ships the named set — adding ``<name>.svg`` files is enough, no code
change. The loader owns the resolution; the design-keeper owns the SVG art.
"""

from __future__ import annotations

import logging
import pathlib

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

logger = logging.getLogger(__name__)

_ICONDIR = pathlib.Path(__file__).with_name("icons")
_PLACEHOLDER = _ICONDIR / "_placeholder.svg"

# Names the nav rail / overlay menu ask for. Listed so the loader can warn
# (debug) when a named SVG isn't shipped yet — the design-keeper fills these.
KNOWN: tuple[str, ...] = (
    "dashboard", "games", "graphics", "display", "drivers",
    "rtx", "profiles", "dlss", "settings", "menu",
)


def icon(name: str, size: int = 20) -> Gtk.Image:
    """Return a ``Gtk.Image`` for the named line-icon, sized to ``size`` px.

    Falls back to ``_placeholder.svg`` when the named SVG isn't shipped yet,
    so a pending icon never blanks the navigation. ``Gtk.Image.new_from_file``
    loads the SVG at its natural size; ``set_pixel_size`` normalises it.
    """
    p = _ICONDIR / f"{name}.svg"
    if not p.is_file():
        logger.debug("icon %r missing — placeholder used", name)
        p = _PLACEHOLDER
    if p.is_file():
        img = Gtk.Image.new_from_file(str(p))
    else:  # even the placeholder absent (shouldn't happen) — empty image
        img = Gtk.Image()
    img.set_pixel_size(size)
    return img
