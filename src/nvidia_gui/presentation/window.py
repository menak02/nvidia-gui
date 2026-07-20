"""Main window — NVIDIA-App-style sidebar + content stack, plus telemetry poller."""

from __future__ import annotations

import logging
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402

from .icons import icon  # noqa: E402
from .views import (  # noqa: E402
    DashboardView,
    DisplayView,
    DriversView,
    GamesView,
    GraphicsView,
    ProfilesView,
    RtxView,
)
from .views_dlss import build_dlss_view  # noqa: E402
from .views_settings import build_settings_view, open_about_dialog  # noqa: E402
from .widgets import Debouncer, NavSidebar, SaveToast, StatusBar  # noqa: E402

logger = logging.getLogger(__name__)

# (name, label, icon_name)  — drives both the sidebar and the stack. The third
# column is a NAME from icons.KNOWN (an original SVG line-icon), NOT an emoji —
# the app-wide no-emoji contract. icons.py resolves the name to
# icons/<name>.svg (placeholder if the art isn't shipped yet).
_PAGES = [
    ("dashboard", "Dashboard", "dashboard"),
    ("games", "Games", "games"),
    ("graphics", "Graphics", "graphics"),
    ("display", "Display", "display"),
    ("drivers", "Drivers", "drivers"),
    ("rtx", "RTX", "rtx"),
    ("profiles", "Profiles", "profiles"),
    ("dlss", "DLSS", "dlss"),
    ("settings", "Settings", "settings"),
]


class TelemetryPoller:
    """Polls nvidia-smi on a worker thread; marshals snapshots to the main loop.

    Never touches GTK on its own thread — it only enqueues an ``idle_add``
    callback that updates the dashboard. A modest back-off prevents a runaway
    loop if snapshots repeatedly error.
    """

    def __init__(self, snapshot_fn, on_snapshot, interval: float = 1.5) -> None:
        self._snapshot_fn = snapshot_fn
        self._on_snapshot = on_snapshot
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self._snapshot_fn()
            except Exception as exc:  # noqa: BLE001
                logger.debug("snapshot error: %s", exc)
                snap = None
            if snap is not None:
                GLib.idle_add(self._on_snapshot, snap)
            # sleep cooperatively, waking early on stop
            self._stop.wait(self._interval)


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app, uc) -> None:
        super().__init__(application=app, title="NVIDIA-GUI")
        self.uc = uc
        # Persisted geometry — read through the settings port. The on-disk
        # config may predate these keys, so every read carries an explicit
        # fallback. A tiling WM overrides the requested size once mapped, but
        # the requested value is the _default/max hint_ GTK honours until then.
        self.set_default_size(
            uc.setting("window.width", 1180),
            uc.setting("window.height", 760),
        )
        # Minimum size request floor allows tiling window managers to scale down
        self.set_size_request(480, 400)
        self.add_css_class("nvidia-gui-window")
        # Animations tier — scope the motion rules on the ROOT window. Called
        # once on boot from the saved preference, and again whenever the
        # Settings page flips the tier. NO theme reload: styles-effects.css is
        # loaded once by theme.py and the CSS-class swap re-scopes which rules
        # match (design-keeper owns the rules; window-keeper owns the class).
        self._on_anim_changed(uc.setting("presentation.animations", "full"))
        self._alive = True  # fences off idle_add callbacks fired after close
        # Last known rail width (clamped), kept live so hiding then showing the
        # sidebar can restore the user's divider and so close can persist it.
        self._sidebar_w = uc.setting("window.sidebar_width", 220)
        self._paned_deb = Debouncer(400)

        # ---- top bar --------------------------------------------------
        topbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        topbar.add_css_class("nvgui-sidebar")
        topbar.set_margin_start(0); topbar.set_margin_end(0)

        # Overlay nav: a hamburger whose popover lists the 9 pages and a
        # "Show sidebar" toggle. It's the navigation path while the rail is
        # collapsed (narrow/tiled window) and lives in the always-visible top
        # bar, so it never depends on the divider's position.
        self._menubtn = Gtk.MenuButton()
        # The hamburger face is the design-keeper's "menu" SVG line-icon —
        # NOT a "trigram" emoji glyph — honouring the no-emoji contract. The
        # icon's stroke is pinned #76b900 in menu.svg itself (see icons.py):
        # Gtk.Image.new_from_file rasterises the SVG once, at pixbuf-load time,
        # with no widget style context — so CSS `color` can't reach the baked
        # pixels. Pinning the green in-file is what makes it visible on #0b0b0b.
        face = icon("menu", size=20)
        self._menubtn.set_child(face)
        self._menubtn.set_tooltip_text("Navigate / hide sidebar")
        self._populate_overlay_menu()
        topbar.append(self._menubtn)

        logo = Gtk.Label(label="NVIDIA-GUI")
        logo.add_css_class("nvgui-nav-title")
        live = Gtk.Label(label="●")
        live.add_css_class("nvgui-status-dot")
        topbar.append(logo)
        topbar.append(Gtk.Label(label=""))  # spacer won't expand; hexpand below

        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title_box.set_hexpand(True)
        title_box.append(Gtk.Label(label=""))
        self._live_label = Gtk.Label(label="", xalign=1)
        self._live_label.add_css_class("nvgui-card-subtle")
        self._live_label.set_ellipsize(Pango.EllipsizeMode.END)
        title_box.append(self._live_label)
        topbar.append(title_box)

        # ---- body: sidebar | stack (flex layout) ----
        self.sidebar = NavSidebar()
        self.sidebar.set_size_request(-1, -1)  # Naturally scale to content (fit-content)
        self.sidebar.set_hexpand(False)
        self.sidebar.set_vexpand(True)
        self.sidebar.set_halign(Gtk.Align.START)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)
        # Page-flip. The slide's RENDERING tier is gated by the root
        # nvgui-motion-{full|minimal|off} class (design-keeper's CSS), so an
        # "off" tier still reads as an instantaneous swap despite this enum.
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)

        # build views lazily so each only constructs its widgets once
        self._views: dict = {}
        self._dashboard = DashboardView(uc)
        # on_status routes save confirmations to the toast, so Ctrl+S (or the
        # Save button) flashes a transient result even off the Games page.
        self._games = GamesView(uc, on_status=self._push_status)
        self._graphics = GraphicsView(uc)
        self._display = DisplayView(uc)
        self._drivers = DriversView(uc, on_navigate=self.sidebar.switch_to)
        self._rtx = RtxView(uc, on_navigate=self.sidebar.switch_to)
        self._profiles = ProfilesView(uc, on_status=self._push_status)
        # The 7 original pages are CLASSES exposing .root(); the 2 new pages
        # (dlss, settings) are built by FACTORIES that return a Gtk.Widget
        # directly (no .root() hop), so stack registration splits on the name.
        self._class_pages = {
            "dashboard": "_dashboard", "games": "_games",
            "graphics": "_graphics", "display": "_display",
            "drivers": "_drivers", "rtx": "_rtx", "profiles": "_profiles",
        }
        for name, _label, _icon in _PAGES:
            if name in self._class_pages:
                view = getattr(self, self._class_pages[name])
                self.stack.add_named(view.root(), name)
            elif name == "dlss":
                self.stack.add_named(
                    build_dlss_view(uc, on_status=self._push_status), name
                )
            elif name == "settings":
                self.stack.add_named(
                    build_settings_view(
                        uc,
                        on_anim_changed=self._on_anim_changed,
                        on_motion_reduce=self._on_motion_reduce_changed,
                    ),
                    name,
                )
        # Restore the last-viewed page (persisted "presentation.active_page").
        # Validated against _PAGES so a name removed in a later release can't
        # leave the sidebar and stack out of sync — falls back to dashboard.
        saved = uc.setting("presentation.active_page", "dashboard")
        active = saved if saved in {n for n, _l, _i in _PAGES} else "dashboard"
        self.stack.set_visible_child_name(active)
        self.sidebar.bind_stack(self.stack)
        for name, label, icon_name in _PAGES:
            self.sidebar.add_item(name, label, icon_name)
        # Persist every page switch — the Stack's visible-child is the single
        # source of truth, so this catches sidebar buttons, the overlay
        # hamburger's switch_to, and future keyboard accelerators alike.
        self.stack.connect("notify::visible-child", self._on_page_changed)

        # ---- assemble flex container ---------------------------------
        # The split box matches display: flex; flex-direction: row
        self._split_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._split_box.set_hexpand(True)
        self._split_box.set_vexpand(True)
        self._split_box.append(self.sidebar)
        self._split_box.append(self.stack)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main.set_hexpand(True)
        main.set_vexpand(True)
        main.append(topbar)
        sep = Gtk.Separator()
        main.append(sep)
        main.append(self._split_box)
        # Status bar: persistent bottom feedback line (last action / error / ipc path)
        self.statusbar = StatusBar()
        main.append(self.statusbar)
        # Save toast: the canonical transient-confirmation surface, mounted AFTER
        # the status bar so it slides UP from the window bottom. A Gtk.Revealer
        # collapses to zero height when hidden, so it reserves no space until a
        # save/swap fires. It respects the motion tier (instant on 'off').
        self.toast = SaveToast()
        self.toast.set_instant(self._motion_off())
        main.append(self.toast)
        self.set_child(main)

        # ---- telemetry -----------------------------------------------
        self._poller = TelemetryPoller(
            snapshot_fn=uc.snapshot,
            on_snapshot=self._on_snapshot,
            interval=1.5,
        )
        self.connect("close-request", self._on_close)
        # an initial synchronous snapshot so the dashboard isn't empty for 1.5s
        GLib.idle_add(self._on_snapshot, uc.snapshot())
        GLib.idle_add(self._initial_load)
        # ---- keyboard accelerators (window-scoped win.* GActions) ----------
        # Registered AFTER self.set_child/main so every delegate target exists
        # (the sidebar, the views, the toast). One action per accel; bound via
        # Gtk.Application.set_accels_for_action so GTK labels them in
        # gtk4-inspector + the shortcuts window. ApplicationWindow IS a
        # GActionMap, so add_action lands the win.* name here; the app binds the
        # keystroke. See _setup_accels for the per-action bindings.
        self._setup_accels(app)

    # ---- animations tier -------------------------------------------------
    def _on_anim_changed(self, tier: str) -> None:
        """Swap the root motion CSS class and persist the tier.

        Called once on boot (to scope effect rules from the saved preference)
        and again whenever the Settings page flips the tier. The class swap is
        the ONLY lever — no theme reload (styles-effects.css is already loaded
        once by theme.py; the class re-scopes which rules match). An unset or
        falsy tier normalises to ``full``.
        """
        if not tier:
            tier = "full"
        self.uc.set_setting("presentation.animations", tier)
        if self.uc.setting("presentation.motion_reduce", False):
            tier = "off"
        self._apply_motion_tier(tier)

    def _on_motion_reduce_changed(self, active: bool) -> None:
        """Handle accessibility motion reduction toggle."""
        tier = "off" if active else self.uc.setting("presentation.animations", "full")
        self._apply_motion_tier(tier)

    def _apply_motion_tier(self, tier: str) -> None:
        """Helper to modify window class list and update components for motion control."""
        for cls in self.get_css_classes():
            if cls.startswith("nvgui-motion-"):
                self.remove_css_class(cls)
        self.add_css_class(f"nvgui-motion-{tier}")
        
        # Update Gtk.Stack transition behavior dynamically based on motion tier
        stack = getattr(self, "stack", None)
        if stack is not None:
            if tier == "off":
                stack.set_transition_type(Gtk.StackTransitionType.NONE)
            elif tier == "minimal":
                stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
                stack.set_transition_duration(60)
            else:
                stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
                stack.set_transition_duration(200)

        # Re-scope the save toast to the new tier (guarded: the boot call at
        # __init__ runs before self.toast is constructed).
        toast = getattr(self, "toast", None)
        if toast is not None:
            toast.set_instant(tier == "off")

    def _motion_off(self) -> bool:
        """True iff the root is currently on the ``off`` motion tier. The toast
        reads this once at mount (the anim handler keeps it in sync after)."""
        return "nvgui-motion-off" in (self.get_css_classes() or [])

    def _push_status(self, message: str) -> None:
        """The surface views reach to show a transient confirmation (the toast).
        Views get a bound callback, never the toast object — so a view can't
        assume anything about the widget that happens to mount the message."""
        self.toast.show(message)
        # Also update the persistent status bar for cross-page feedback
        if hasattr(self, "statusbar"):
            self.statusbar.push(message)

    # ---- keyboard accelerators ----------------------------------------------
    def _setup_accels(self, app) -> None:
        """Bind keyboard accelerators to window-scoped ``win.*`` GActions.

        GTK4 replaced ``GtkAccelGroup`` with the GAction model: each
        ``Gio.SimpleAction`` is hosted on this ``Gtk.ApplicationWindow`` (which
        IS a ``GActionMap``), and the ``Gtk.Application`` maps keystrokes to
        them via ``set_accels_for_action``. One action per accel keeps the
        bindings explicit and discoverable in gtk4-inspector (a parameterised
        action would need ``GVariant`` targets — more moving parts for 9 fixed
        pages). Every handler delegates to an EXISTING method so there's one
        real code path per operation (``sidebar.switch_to``, games scan/save,
        the About dialog); the accelerator never re-implements behaviour. The
        Ctrl+N page lambda pins ``name=pname`` by default-arg so the loop
        binding doesn't collapse to the last page for all 9 (Python's late-
        binding-on-closures trap).
        """
        def _mk(name, handler, accels):
            act = Gio.SimpleAction.new(name, None)  # stateless, no GVariant param
            act.connect("activate", handler)
            self.add_action(act)
            app.set_accels_for_action(f"win.{name}", list(accels))

        _mk("scan", self._act_scan, ["<Control>R"])
        _mk("save", self._act_save, ["<Control>S"])
        _mk("about", self._act_about, ["F1"])
        _mk("escape", self._act_escape, ["Escape"])
        for i, (pname, _label, _icon) in enumerate(_PAGES, start=1):
            _mk(f"page{i}",
                lambda _a, _p, name=pname: self.sidebar.switch_to(name),
                [f"<Control>{i}"])

    def _act_scan(self, _action, _param) -> None:
        """Ctrl+R — the same guarded async scan the Scan button runs. The
        ``GamesView.refresh`` ``_scanning`` guard stops a re-trigger into a
        thread pile-up if Ctrl+R is held."""
        self._games.refresh()

    def _act_save(self, _action, _param) -> None:
        """Ctrl+S — commit the current Games editor through the single save
        seam (the Save button's own path). A no-op when no game is selected, so
        Ctrl+S off the Games page with no pinned editor surprises nobody."""
        self._games.trigger_save()

    def _act_about(self, _action, _param) -> None:
        """F1 — the About dialog (program, version, license, credits). Parent
        is this window so the modal stacks over the app on a multi-monitor
        setup; ``open_about_dialog`` never raises, so a fault here can't strand
        an accelerator."""
        open_about_dialog(self, self.uc.version())

    def _act_escape(self, _action, _param) -> None:
        """Esc — fold the overlay hamburger popover if it's open. Modal dialogs
        (About, the confirm AlertDialogs) own their own Escape handling — they
        close on Esc — so this only fires when the popover is the topmost
        interactive surface, never clobbering a dialog's own Escape."""
        popover = self._menubtn.get_popover()
        if popover is not None:
            popover.popdown()

    def _on_page_changed(self, stack, _pspec) -> None:
        """Persist ``presentation.active_page`` on every page switch.

        Fires for sidebar clicks, the overlay hamburger's ``switch_to``, and
        any future keyboard accel that flips the Stack — one seam, all drivers.
        Guarded: a None child (shouldn't happen, but the Stack yields None if no
        child set) just no-ops rather than persisting an empty name.
        """
        name = stack.get_visible_child_name()
        if name:
            self.uc.set_setting("presentation.active_page", name)

    # ---- overlay nav menu ----------------------------------------------
    def _populate_overlay_menu(self) -> None:
        """The hamburger popover: the 9 pages + a "Show sidebar" check.

        Each page button flips the stack via :meth:`NavSidebar.switch_to` — the
        SAME path a rail press takes — so the active-row highlight always agrees
        with the visible page whichever control drove it. Buttons are label-
        only (no emoji prefix); the rail renders the SVG icons, the popover
        lists the page names."""
        popover = Gtk.Popover()
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.set_margin_start(10); vbox.set_margin_end(10)
        vbox.set_margin_top(8); vbox.set_margin_bottom(8)
        for name, label, _icon in _PAGES:
            b = Gtk.Button(label=label)
            b.add_css_class("nvgui-nav-btn-flat")
            b.connect("clicked", lambda _w, n=name: self._goto(n))
            vbox.append(b)
        # "Show sidebar" check — hides the rail so content fully expands; the
        # hamburger remains the only nav path while collapsed.
        self._sidebar_check = Gtk.CheckButton(label="Show sidebar")
        self._sidebar_check.set_active(True)
        self._sidebar_check.connect("toggled", self._on_toggle_sidebar)
        vbox.append(Gtk.Separator())
        vbox.append(self._sidebar_check)
        popover.set_child(vbox)
        self._menubtn.set_popover(popover)

    def _goto(self, name: str) -> None:
        """Flip the stack through the sidebar's public entry point, then close
        the popover (MenuButton keeps it open otherwise)."""
        self.sidebar.switch_to(name)
        popover = self._menubtn.get_popover()
        if popover is not None:
            popover.popdown()

    def _on_toggle_sidebar(self, chk) -> None:
        self.sidebar.set_visible(chk.get_active())


    def _initial_load(self) -> bool:
        """Populate the heavier views once, on the first idle tick."""
        try:
            self._games.refresh()
            self._drivers.refresh()
        except Exception as exc:  # noqa: BLE001
            logger.warning("initial load: %s", exc)
        # start telemetry polling now that the main loop is running
        self._poller.start()
        return False  # one-shot

    def _on_snapshot(self, snap) -> bool:
        if not self._alive:
            return False  # window closing; drop a telemetry callback queued before stop()
        self._dashboard.update(snap)
        dev = (snap.gpu_util_pct + "%") if snap.gpu_util_pct else "—"
        tmp = (snap.temperature_c + "°C") if snap.temperature_c else "—"
        self._live_label.set_text(
            f"{dev}  ·  {snap.gpu_name.split('GeForce ')[-1]}  ·  {tmp}"
        )
        return False  # one-shot for idle_add; the poller feeds us new ones

    def _on_close(self, _w) -> None:
        self._alive = False  # before stop(): a queued idle fired now returns early
        self._poller.stop()
        self._paned_deb.flush()
        # also flush the games-list divider's pending persist (defensive: other
        # views have no flush(); only GamesView owns a draggable inner Paned)
        getattr(self._games, "flush", lambda: None)()
        # Pin the final window size (allocated = whatever the WM landed on, so a
        # a tiled relaunch defaults near the tile) and the last rail width. A
        # persist failure must never block shutdown.
        try:
            self.uc.set_setting("window.width", self.get_width())
            self.uc.set_setting("window.height", self.get_height())
            self.uc.set_setting("window.sidebar_width", self._sidebar_w)
        except Exception as exc:  # noqa: BLE001
            logger.debug("persist geometry failed: %s", exc)
