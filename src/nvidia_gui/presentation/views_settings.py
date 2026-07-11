"""Settings page -- animations tier, motion-reduce, and About.

design-keeper owns this page's *content* (the window-keeper *registers* it in
``window.py``). The animations tier maps to the root CSS class
``nvgui-motion-{full|minimal|off}``: choosing a tier here both persists the
setting (``presentation.animations``) and notifies the window via
``on_anim_changed(tier)`` so it can swap the root class -- no theme reload, the
effect rules in ``styles-effects.css`` re-scope on the class change.

Hexagonal purity: this module imports only PyGObject (``gi``), a sibling
presentation module (``widgets``), and -- under TYPE_CHECKING only -- the
application ``UseCases``. No adapter is imported; ``uc`` is the seam.

Signature fixed: ``build_settings_view(uc, on_anim_changed=None) -> Gtk.Widget``.
``_card`` / ``_scrolled`` are reimplemented locally (mirroring ``views.py``) so
this page never imports a private from another view -- visual parity without a
formatter dependency.
"""

from __future__ import annotations

from typing import Callable, TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .widgets import ToggleRow, pill  # noqa: E402  sibling presentation module

if TYPE_CHECKING:
    from ..application.use_cases import UseCases

# value -> label. value is the persisted token; label is what the user sees.
_ANIM_TIERS: tuple[tuple[str, str], ...] = (
    ("full", "Full"),
    ("minimal", "Minimal"),
    ("off", "Off"),
)
_ANIM_DEFAULT = "full"


def _card(title: str, subtitle: str = "") -> tuple[Gtk.Box, Gtk.Box]:
    """Return (card, body) so callers append rows to body.

    Local mirror of ``views._card``: the same nvgui-card / nvgui-card-header /
    nvgui-card-title / nvgui-corner / nvgui-card-subtle classes, so this page's
    cards are visually identical to every other page's without importing the
    private helper.
    """
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    card.add_css_class("nvgui-card")
    head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    head.add_css_class("nvgui-card-header")
    head.set_hexpand(True)
    t = Gtk.Label(label=title, xalign=0)
    t.add_css_class("nvgui-card-title")
    t.set_hexpand(True)
    head.append(t)
    dot = Gtk.Box()
    dot.add_css_class("nvgui-corner")
    head.append(dot)
    card.append(head)
    if subtitle:
        s = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        s.add_css_class("nvgui-card-subtle")
        card.append(s)
    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    card.append(body)
    return card, body


def _scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    """Local mirror of ``views._scrolled``: same margins + spacing for parity."""
    sw = Gtk.ScrolledWindow()
    sw.set_vexpand(True)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.set_margin_top(20)
    box.set_margin_bottom(20)
    box.set_margin_start(20)
    box.set_margin_end(20)
    box.append(child)
    sw.set_child(box)
    return sw


def build_settings_view(uc: "UseCases",
                        on_anim_changed: Callable[[str], None] | None = None,
                        ) -> Gtk.Widget:
    """Build the Settings page: animations tier, motion-reduce, and About.

    * Animations tier -- three mutually-exclusive radios (Full/Minimal/Off).
      Selecting one persists ``presentation.animations`` and calls
      ``on_anim_changed(tier)`` (guarded for None, swallows handler errors so a
      view-side fault can't break the settings write).
    * Motion-reduce -- a Gtk.Switch (via ``ToggleRow``) persisting
      ``presentation.motion_reduce`` (bool).
    * About -- the brand wordmark "NVIDIA-GUI", a one-line description, a
      version placeholder, and the single trademark-disclaimer line. No
      "GeForce App" / "NVIDIA App" endorsement wordmarks; RTX/DLSS/Reflex are
      used only nominatively (as feature labels, not endorsement).
    """
    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

    # ---- page title ------------------------------------------------------
    title = Gtk.Label(label="Settings")
    title.add_css_class("nvgui-nav-title")
    title.set_xalign(0)
    title.set_halign(Gtk.Align.START)
    root.append(title)

    # ---- Animations tier -------------------------------------------------
    anim_card, anim_body = _card(
        "Animations",
        "Interface motion tier. Full enables card entrances and the ambient GPU-status pulse; Minimal keeps only fast transitions; Off disables all motion.",
    )
    current = uc.setting("presentation.animations", _ANIM_DEFAULT)

    radio_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
    radios: dict[str, Gtk.CheckButton] = {}
    first: Gtk.CheckButton | None = None
    for value, label in _ANIM_TIERS:
        cb = Gtk.CheckButton(label=label)
        if first is None:
            first = cb
        else:
            cb.set_group(first)  # GTK4 radio group membership
        radios[value] = cb
    # set the active radio BEFORE wiring signals so page-open doesn't re-write
    active_key = current if current in radios else _ANIM_DEFAULT
    radios[active_key].set_active(True)

    def _on_tier_toggled(btn: Gtk.CheckButton, tier: str) -> None:
        # "toggled" fires on both activate and deactivate; only the newly-active
        # radio (get_active() True) is a real selection.
        if not btn.get_active():
            return
        uc.set_setting("presentation.animations", tier)
        if on_anim_changed is not None:
            try:
                on_anim_changed(tier)
            except Exception:  # noqa: BLE001
                pass  # a window-side handler fault must not abort the persist

    for value, _label in _ANIM_TIERS:
        radios[value].connect("toggled", _on_tier_toggled, value)
        radio_row.append(radios[value])
    anim_body.append(radio_row)
    root.append(anim_card)

    # ---- Motion-reduce ---------------------------------------------------
    mr_card, mr_body = _card(
        "Accessibility",
        "Reduce motion mirrors the Off tier (instant changes, no entrance or pulse) and remembers the choice across launches.",
    )
    mr_active = bool(uc.setting("presentation.motion_reduce", False))
    mr_row = ToggleRow(
        "Reduce motion",
        subtitle="Disable card-entrance and status-dot animations.",
        active=mr_active,
    )

    def _on_motion_reduce(_row: ToggleRow, active: bool) -> None:
        uc.set_setting("presentation.motion_reduce", bool(active))

    mr_row.connect("toggled", _on_motion_reduce)
    mr_body.append(mr_row)
    root.append(mr_card)

    # ---- About -----------------------------------------------------------
    about_card, about_body = _card("About", "")
    brand = Gtk.Label(label="NVIDIA-GUI", xalign=0)
    brand.add_css_class("nvgui-nav-title")  # the brand-green wordmark treatment
    about_body.append(brand)

    desc = Gtk.Label(
        xalign=0,
        wrap=True,
        label="A Linux GPU control center: live telemetry, driver and "
              "kernel-module controls, DLSS overrides, and per-game launch "
              "injection.",
    )
    desc.add_css_class("nvgui-row-value")
    about_body.append(desc)

    version = Gtk.Label(label="Version 1.0.0", xalign=0)  # placeholder
    version.add_css_class("nvgui-muted")
    version.set_margin_top(6)
    about_body.append(version)

    # license line uses widgets.pill (the nvgui-pill badge class)
    license_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    license_box.set_margin_top(6)
    license_box.append(pill("MIT", on=True))
    lic_note = Gtk.Label(label="Open source project", xalign=0)
    lic_note.add_css_class("nvgui-muted")
    license_box.append(lic_note)
    about_body.append(license_box)

    # the single trademark disclaimer line (nominative use only)
    disclaimer = Gtk.Label(
        xalign=0,
        wrap=True,
        label="Unaffiliated with NVIDIA Corporation. NVIDIA, GeForce, RTX, "
              "DLSS are trademarks of NVIDIA Corp.",
    )
    disclaimer.set_margin_top(8)
    disclaimer.add_css_class("nvgui-muted")
    about_body.append(disclaimer)
    root.append(about_card)

    return _scrolled(root)
