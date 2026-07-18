"""Reusable(GTK4 widgets: stat graph, toggle row, nav sidebar, cards.

All drawn with solid Cairo strokes / solid CSS — nothing that the compositor
has to blur or gradient. The live graph keeps a rolling buffer and redraws on
``queue_draw`` from the main loop; it never polls nvidia-smi itself.
"""

from __future__ import annotations

import collections
import logging

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject, Gtk  # noqa: E402

from .icons import icon

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Live stat graph — Cairo solid stroke, rolling buffer
# ---------------------------------------------------------------------------
class StatGraph(Gtk.DrawingArea):
    """A tiny live line graph. Push samples via :meth:`push`.

    Keeps the last ``max_samples`` values and draws them as a green poly-line
    inside the 1px-bordered box. No fill gradient — outlined area only, which
    the compositor renders as flat triangles (cheap, no shader stall).
    """

    def __init__(self, label: str = "", max_samples: int = 90) -> None:
        super().__init__()
        self._label = label
        self._buf: collections.deque[float] = collections.deque(maxlen=max_samples)
        self._max = max_samples
        self.set_content_width(300)
        self.set_content_height(78)
        self.add_css_class("nvgui-card")
        self.set_draw_func(self._draw, None)

    def push(self, value: float | None) -> None:
        if value is None:
            return
        try:
            f = float(value)
        except (TypeError, ValueError):
            return
        self._buf.append(f)
        self.queue_draw()

    def _draw(self, _area, cr, width: int, height: int, _user) -> None:
        # background already from CSS card; draw border + line
        cr.set_source_rgba(0x26 / 255, 0x26 / 255, 0x26 / 255, 1.0)
        cr.set_line_width(1.0)
        cr.rectangle(1, 1, width - 2, height - 2)
        cr.stroke()
        if len(self._buf) < 2:
            return
        lo = min(self._buf)
        hi = max(self._buf)
        if hi - lo < 1e-3:
            hi = lo + 1.0
        n = len(self._buf)
        dx = (width - 4) / max(1, self._max - 1)
        # baseline 0-reference line
        cr.set_source_rgba(0x26 / 255, 0x26 / 255, 0x26 / 255, 0.6)
        cr.set_line_width(0.5)
        cr.move_to(2, height - 3)
        cr.line_to(width - 2, height - 3)
        cr.stroke()
        # the data line (green)
        cr.set_source_rgba(0x76 / 255, 0xB9 / 255, 0.0 / 255, 1.0)
        cr.set_line_width(1.6)
        for i, v in enumerate(self._buf):
            x = 2 + i * dx
            y = (height - 6) - ((v - lo) / (hi - lo)) * (height - 10) - 2
            if i == 0:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)
        cr.stroke()


# ---------------------------------------------------------------------------
#  Toggle row — label + value + Gtk.Switch (or inline edit)
# ---------------------------------------------------------------------------
class ToggleRow(Gtk.Box):
    """A labelled row with a switch. Emits ``toggled`` via the real switch."""

    __gsignals__ = {
        "toggled": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
    }

    def __init__(self, label: str, subtitle: str = "", active: bool = False) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.set_homogeneous(False)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        left.set_hexpand(True)
        l = Gtk.Label(label=label, xalign=0)
        l.add_css_class("nvgui-row-label")
        left.append(l)
        if subtitle:
            s = Gtk.Label(label=subtitle, xalign=0, wrap=True)
            if subtitle.startswith("DXVK_") or subtitle.startswith("PROTON_") or subtitle.startswith("VKD3D_"):
                s.add_css_class("nvgui-env-var")
            else:
                s.add_css_class("nvgui-card-subtle")
            left.append(s)
        self.append(left)
        self._sw = Gtk.Switch()
        self._sw.set_active(active)
        self._sw.set_valign(Gtk.Align.CENTER)
        self._update_style(active)
        self._sw.connect("notify::active", self._on_active)
        self.append(self._sw)

    def _on_active(self, _sw, _pspec) -> None:
        active = self._sw.get_active()
        self.emit("toggled", active)

    def _update_style(self, _active: bool) -> None:
        # State is communicated entirely by the switch track/thumb colour —
        # no row-level class manipulation needed. Kept as a no-op so subclasses
        # or future callers that override it don't break.
        pass

    @property
    def active(self) -> bool:
        return self._sw.get_active()

    def set_active(self, val: bool) -> None:
        self._sw.set_active(val)
        self._update_style(val)


class TextRow(Gtk.Box):
    """A label + readonly value row (compact: value sits immediately right of key)."""

    def __init__(self, key: str, value: str = "", accent: bool = False) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        k = Gtk.Label(label=key, xalign=0)
        k.add_css_class("nvgui-row-label")
        k.set_size_request(120, -1)  # fixed key width — prevents value jumping
        v = Gtk.Label(label=value or "—", xalign=0)
        v.add_css_class("nvgui-row-value")
        if accent:
            v.add_css_class("accent")
        self.append(k)
        self.append(v)
        self._value = v

    def set_value(self, value: str) -> None:
        self._value.set_text(value or "—")


# ---------------------------------------------------------------------------
#  Pill (status badge)
# ---------------------------------------------------------------------------
def pill(text: str, on: bool = True) -> Gtk.Box:
    b = Gtk.Box(spacing=8)
    p = Gtk.Label(label=text)
    p.add_css_class("nvgui-pill" if on else "nvgui-pill off")
    b.append(p)
    return b


# ---------------------------------------------------------------------------
#  toplevel_of — resolve a clicked widget to its window (for modal parenting)
# ---------------------------------------------------------------------------
def toplevel_of(widget: Gtk.Widget) -> Gtk.Window | None:
    """Resolve a clicked widget to its enclosing ``Gtk.Window``.

    ``Gtk.AlertDialog.choose`` wants a transient parent so the modal stacks over
    the right window (on a multi-monitor setup an unparented alert may surface
    on the WM's focused monitor, not the app's own). ``get_root()`` walks up to
    the containing window. Returns ``None`` on any mismatch rather than raising
    — :func:`confirm_destructive` tolerates a ``None`` parent (the alert is
    then unparented but still modal, so the confirm is never blocked by a bad
    parent lookup).
    """
    root = widget.get_root()
    return root if isinstance(root, Gtk.Window) else None


# ---------------------------------------------------------------------------
#  confirm_destructive — async modal YES/NO for destructive actions (GTK 4.10+)
# ---------------------------------------------------------------------------
def confirm_destructive(
    parent: Gtk.Window | None,
    message: str,
    detail: str = "",
    *,
    confirm_label: str = "Confirm",
    on_confirm=None,
) -> None:
    """Modal confirm for a destructive action. Async, never blocks.

    ``gtk_dialog_run()`` is gone in GTK4 (re-entrancy), so a confirm is an
    async ``Gtk.AlertDialog.choose()`` + a ``Gio.AsyncReadyCallback``.
    ``on_confirm()`` runs ONLY when the user picks the affirmative button
    (the LAST entry in ``buttons``, matching GTK's convention that the
    destructive action is right-most); cancel/dismiss/ESC run nothing. The
    helper never raises -- a finish/dismiss failure just skips the action.

    ``parent`` may be ``None`` (the alert is then unparented; still modal).
    Verified on GTK 4.22; ``Gtk.AlertDialog`` arrived in 4.10, so this needs
    GTK >= 4.10 (any 2023+ distro ships that).
    """
    dlg = Gtk.AlertDialog()
    dlg.set_message(message)
    if detail:
        dlg.set_detail(detail)
    buttons = ["Cancel", confirm_label]
    dlg.set_buttons(buttons)

    def _on_choose(_dialog, res) -> None:
        try:
            idx = dlg.choose_finish(res)
        except Exception:  # noqa: BLE001 -- dismissed/failed -> no action
            return
        # right-most button (len-1) == the affirmative/confirm choice
        if idx == len(buttons) - 1 and on_confirm is not None:
            try:
                on_confirm()
            except Exception as exc:  # noqa: BLE001 -- the caller's action must never crash the idle cb
                logger.warning("confirm_destructive on_confirm raised: %s", exc)

    dlg.choose(parent, None, _on_choose, None)


# ---------------------------------------------------------------------------
#  Debouncer — coalesce a burst of GTK signals into one trailing write
# ---------------------------------------------------------------------------
class Debouncer:
    """Run ``fn`` once after *ms* of signal quiet, cancelling any pending run.

    ``Gtk.Paned`` emits ``notify::position`` on every pixel of a divider drag —
    hundreds of times a second. We don't want to rewrite ``config.toml`` each
    tick (it's an atomic tmp+rename per set). Schedule the persist; if a new
    position arrives before *ms* elapses, the old timer is removed and a fresh
    one starts, so only the final settled value is written. ``flush`` runs a
    pending instance immediately (used on window close to pin the last value).
    """

    def __init__(self, ms: int = 400) -> None:
        self._ms = ms
        self._id = 0
        self._pending = None

    def schedule(self, fn) -> None:
        self.cancel()
        self._pending = fn
        self._id = GLib.timeout_add(self._ms, self._fire, fn)

    def _fire(self, fn) -> bool:
        self._id = 0
        self._pending = None
        try:
            fn()
        except Exception:  # noqa: BLE001 — a persist failure must not crash the UI
            logger.debug("debouncer fn raised", exc_info=True)
        return False  # one-shot (return False → GLib drops the source)

    def flush(self) -> None:
        """Run a pending instance now and detach it (used on close)."""
        pending, self._pending = self._pending, None
        if self._id:
            GLib.source_remove(self._id)
            self._id = 0
        if pending is not None:
            try:
                pending()
            except Exception:  # noqa: BLE001
                logger.debug("debouncer flush raised", exc_info=True)

    def cancel(self) -> None:
        if self._id:
            GLib.source_remove(self._id)
            self._id = 0
        self._pending = None


# ---------------------------------------------------------------------------
#  SaveToast — transient confirmation popup (Gtk.Revealer) at the window bottom
# ---------------------------------------------------------------------------
class SaveToast(Gtk.Revealer):
    """A transient confirmation popup ("Profile saved", "Swap applied") that
    slides up at the window bottom, holds ~3s, then retracts.

    This mounts the previously-dead ``styles-effects.css`` ``revealer`` styling
    and replaces the per-view bare inline-label feedback with one canonical
    surface. Crash-safe per the app contract: the toast is solid CSS fills only
    (``.nvgui-toast``), and its reveal is a GTK *built-in* transition (not a CSS
    opacity/blur) — so it never stalls the NVIDIA Wayland compositor.

    Motion-tier aware: the window calls :meth:`set_instant` when the tier
    changes. In ``instant`` mode (the ``off`` tier) the built-in transition is
    set to ``NONE`` so the reveal is immediate — the CSS ``revealer`` transition
    in effects.css only animates the node's own colour shift, which is already
    gated to nothing by ``nvgui-motion-off *``. In modes (``full``/``minimal``)
    the slide-up plays.

    Thread-safe enough for the UI path: :meth:`show` may be called from a
    ``GLib.idle_add`` callback (the only off-main-loop path that feeds it —
    thematically mirroring :class:`Debouncer` and :class:`TelemetryPoller`).
    """

    def __init__(self, timeout_ms: int = 2800) -> None:
        super().__init__()
        self._timeout = timeout_ms
        self._hide_id = 0
        self._instant = False
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_reveal_child(False)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.END)
        # IMPORTANT: the nvgui-toast CSS class is on an INNER box, NOT on the
        # Revealer itself. A Revealer with CSS background/border/padding on its
        # own node renders the box at the bottom even when reveal_child=False
        # (the Revealer collapses its child's allocated space but still paints
        # its own CSS box). Inner-box approach: Revealer is fully transparent;
        # only the inner box carries the dark-green tinted background.
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        inner.add_css_class("nvgui-toast")
        self._label = Gtk.Label(label="", xalign=0.5, wrap=True)
        self._label.add_css_class("nvgui-toast-label")
        inner.append(self._label)
        self.set_child(inner)

    def set_instant(self, instant: bool) -> None:
        """Motion-tier switch: instant (the ``off`` tier) skips the slide."""
        self._instant = bool(instant)
        self.set_transition_type(
            Gtk.RevealerTransitionType.NONE if self._instant
            else Gtk.RevealerTransitionType.SLIDE_UP
        )

    def show(self, message: str, timeout_ms: int | None = None) -> None:
        """Reveal *message*, then retract after the timeout. A second show()
        cancels any pending retract and restarts the timer (most-recent wins).
        Empty/falsy message is a no-op — never surface a blank toast."""
        if not message:
            return
        self._cancel_hide()
        self._label.set_text(message)
        self.set_reveal_child(True)
        ms = self._timeout if timeout_ms is None else int(timeout_ms)
        self._hide_id = GLib.timeout_add(ms, self._hide)

    def dismiss(self) -> None:
        """Retract immediately and drop the pending timer."""
        self._cancel_hide()
        self.set_reveal_child(False)

    def _hide(self) -> bool:
        self._hide_id = 0
        self.set_reveal_child(False)
        return False  # one-shot

    def _cancel_hide(self) -> None:
        if self._hide_id:
            GLib.source_remove(self._hide_id)
            self._hide_id = 0


# ---------------------------------------------------------------------------
#  StatusBar — bottom-bar feedback surface (last action / error / ipc path)
# ---------------------------------------------------------------------------
class StatusBar(Gtk.Box):
    """Status bar at the window bottom: last action, error, or IPC socket path.

    Mounted as a child of the main window's root box, respects the motion tier
    (instant reveal on 'off', subtle fade on 'full'/'minimal'). Uses solid CSS
    fills per the crash-safe contract.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.set_homogeneous(False)
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.END)
        self.add_css_class("nvgui-statusbar")
        # Left: status label (action / error / ipc path)
        self._status = Gtk.Label(label="")
        self._status.set_hexpand(True)
        self._status.set_xalign(0)
        self._status.add_css_class("nvgui-status-label")
        self.append(self._status)

    def push(self, message: str) -> None:
        """Update the status text. Empty/clear message resets to baseline."""
        self._status.set_text(message)


# ---------------------------------------------------------------------------
#  Nav sidebar — vertical button list that switches a Gtk.Stack
# ---------------------------------------------------------------------------
class NavSidebar(Gtk.Box):
    """Buttons that flip a connected Stack's visible child."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.set_vexpand(True)
        self.add_css_class("nvgui-sidebar")
        self._stack: Gtk.Stack | None = None
        self._buttons: dict[str, Gtk.Button] = {}
        title = Gtk.Label(label="NVIDIA-GUI")
        title.add_css_class("nvgui-nav-title")
        sub = Gtk.Label(label="Linux GPU Control Center")
        sub.add_css_class("nvgui-nav-subtitle")
        self.append(title)
        self.append(sub)

    def bind_stack(self, stack: Gtk.Stack) -> None:
        self._stack = stack
        stack.connect("notify::visible-child", self._sync_from_stack)

    def add_item(self, name: str, label: str, icon_name: str = "") -> None:
        """Append a nav entry. ``icon_name`` is a *named* original SVG line-icon
        (resolved via :func:`nvidia_gui.presentation.icons.icon`), NOT an emoji —
        the app-wide no-emoji contract. Empty ``icon_name`` ⇒ text-only entry.
        The architect owns this rendering; callers pass names from ``icons.KNOWN``.
        """
        btn = Gtk.Button()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        if icon_name:
            content.append(icon(icon_name, size=20))
        lbl = Gtk.Label(label=label, xalign=0, halign=Gtk.Align.START)
        lbl.set_hexpand(True)
        content.append(lbl)
        btn.set_child(content)
        btn.add_css_class("nvgui-nav-btn")
        btn.connect("clicked", lambda _b, n=name: self._switch(n))
        self._buttons[name] = btn
        self.append(btn)

    def _switch(self, name: str) -> None:
        if self._stack is None:
            return
        self._stack.set_visible_child_name(name)
        self._sync(name)

    def switch_to(self, name: str) -> None:
        """Public entry point for OUT-of-rail navigation (the overlay hamburger
        menu) — mirrors the path a sidebar button press takes so the stack flip
        + active-button highlight stay in lockstep with the rail either way."""
        self._switch(name)

    def _sync(self, active: str) -> None:
        for n, btn in self._buttons.items():
            if n == active:
                btn.add_css_class("active")
            else:
                btn.remove_css_class("active")

    def _sync_from_stack(self, stack, _pspec) -> None:
        child = stack.get_visible_child_name()
        if child:
            self._sync(child)
