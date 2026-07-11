"""Load the NVIDIA theme CSS (static base + motion effects), safe fallback.

Split (design-keepener) into two CssProvider instances, both mounted once at
``STYLE_PROVIDER_PRIORITY_APPLICATION`` so there is no per-toggle reload -- the
nvgui-motion-{full|minimal|off} class on the root window re-scopes all effects.

* ``styles-base.css``  -- the always-on dark NVIDIA aesthetic. Has an in-memory
  fallback so the app still styles if its file is missing or garbled.
* ``styles-effects.css`` -- motion-only rules, every one gated by the motion
  tier. Optional and silent on miss (effects simply don't load; no fallback).

The public shape ``load_theme()`` (no args) is unchanged -- ``window.py`` calls
it exactly that way, so this refactor is invisible to the window-keeper.
"""

from __future__ import annotations

import logging
import pathlib

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

logger = logging.getLogger(__name__)

_BASE_PATH = pathlib.Path(__file__).with_name("styles-base.css")
_EFFECTS_PATH = pathlib.Path(__file__).with_name("styles-effects.css")

_FALLBACK_CSS = """
window { background-color: #000000; }
* { color: #f2f2f2; font-family: Inter, Roboto, sans-serif; font-size: 14px; background: transparent; }
.nvgui-card { background: #141414; border: 1px solid #262626; border-radius: 2px; padding: 16px; margin: 10px 0; }
.nvgui-card-title { color: #ffffff; font-weight: 700; padding-bottom: 6px; }
.nvgui-btn-primary { background: #76b900; color: #0b0b0b; border-radius: 2px; font-weight: 700; }
button { background: #1c1c1c; border: 1px solid #2e2e2e; border-radius: 2px; color: #f2f2f2; }
.nvgui-nav-btn.active { background: #1a1a1a; color: #ffffff; border-left: 3px solid #76b900; }
.nvgui-nav-title { color: #76b900; font-size: 1.4rem; font-weight: 800; padding: 8px; }
.nvgui-pill { background: #162012; color: #9ad815; border-radius: 2px; padding: 2px 8px; }
"""


def _add_provider(provider: Gtk.CssProvider) -> None:
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


def _fallback_provider() -> Gtk.CssProvider:
    """The in-memory dark-CSS escape hatch, with its own escape hatch.

    If the constant itself ever breaks (a future edit adds an experimental
    variable and trips the parser), mount NO provider rather than crash
    startup -- the app runs unstyled but alive. A broken fallback must never
    take the whole window down.
    """
    p = Gtk.CssProvider()
    try:
        p.load_from_data(_FALLBACK_CSS.encode())
    except GLib.Error as exc:
        logger.critical("fallback CSS ALSO failed to parse (%s); "
                        "running unstyled", exc)
        return Gtk.CssProvider()  # empty provider: unstyled but no crash
    return p


def load_theme() -> None:
    """Install base + effects CSS on the default display (no args).

    Base has an in-memory fallback so the app renders sanely if its file is
    missing or fails to parse; effects is optional and silently skipped on any
    miss/parse error (no fallback -- a missing effects file just means no
    motion, which is the correct degraded behaviour).
    """
    # ---- base (always mounted, with fallback) -----------------------------
    base = Gtk.CssProvider()
    if _BASE_PATH.is_file():
        try:
            base.load_from_path(str(_BASE_PATH))
            logger.info("theme base: %s", _BASE_PATH)
        except GLib.Error as exc:
            logger.error("base CSS load failed (%s); using fallback", exc)
            base = _fallback_provider()
    else:
        logger.warning("styles-base.css missing; using fallback")
        base = _fallback_provider()
    _add_provider(base)

    # ---- effects (optional, silent on miss) -------------------------------
    if _EFFECTS_PATH.is_file():
        effects = Gtk.CssProvider()
        try:
            effects.load_from_path(str(_EFFECTS_PATH))
            logger.info("theme effects: %s", _EFFECTS_PATH)
            _add_provider(effects)
        except GLib.Error as exc:
            logger.error("effects CSS load failed (%s); effects disabled", exc)
    else:
        logger.debug("styles-effects.css missing; effects disabled")
