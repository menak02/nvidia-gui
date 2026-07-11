"""All sidebar views. Each builds a GTK4 page widget from the use cases.

Views are deliberately dumb about persistence: they read/write through
:class:`~nvidia_gui.application.use_cases.UseCases` and never reach into
adapters directly. Telemetry updates arrive via ``GLib.idle_add`` callbacks
set up by the main window.
"""

from __future__ import annotations

import logging
import pathlib
import threading
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from ..domain.models import (
    DiagStatus,
    DlssPreset,
    FeatureFlag,
    FeatureSource,
    Game,
    GameCapability,
)
from .download_worker import StreamlineDownloader
from .widgets import Debouncer, StatGraph, TextRow, ToggleRow, pill

if TYPE_CHECKING:
    from ..application.use_cases import UseCases

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Feature-gating — the kinds that are game-support-specific (detected
#  per-game). Gameplay/broadly-available toggles (gamemode, mangohud, nvapi,
#  dxvk_nvapi, expose_gpu) are NEVER gated: every Proton game can use them.
# ---------------------------------------------------------------------------
_FEATURE_KINDS: tuple[tuple[str, str], ...] = (
    ("dlss_sr", "DLSS-SR"),
    ("dlss_fg", "DLSS-G"),
    ("reflex", "Reflex"),
    ("rt", "RT/DXR"),
)


def _feature_supported(cap: GameCapability, kind: str) -> bool:
    """True iff this feature is both reported supported AND from a real source
    (not the UNKNOWN fallback). An override short-circuits this in the caller."""
    f: FeatureFlag = getattr(cap, kind)
    return bool(f.supported) and f.source != FeatureSource.UNKNOWN


def _feature_source_label(flag: FeatureFlag) -> str:
    """Human-readable provenance for a feature chip, e.g. 'curated'."""
    table = {
        FeatureSource.OVERRIDE: "override",
        FeatureSource.ONLINE: "community-db",
        FeatureSource.BUNDLED: "curated",
        FeatureSource.INSTALLDIR: "install-dir",
        FeatureSource.PREFIX: "prefix",
        FeatureSource.UNKNOWN: "unknown",
    }
    return table.get(flag.source, flag.source)


def _card(title: str, subtitle: str = "") -> tuple[Gtk.Box, Gtk.Box]:
    """Return (card, body) so callers append rows to body."""
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
    sw = Gtk.ScrolledWindow()
    sw.set_vexpand(True)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.set_margin_top(20); box.set_margin_bottom(20)
    box.set_margin_start(20); box.set_margin_end(20)
    box.append(child)
    sw.set_child(box)
    return sw


# ---------------------------------------------------------------------------
#  Shared view helpers (clear-then-rebuild idiom + DLSS seed/preset wiring)
# ---------------------------------------------------------------------------
# DLSS preset UI, derived once from DlssPreset.CHOICES so the game editor's
# DropDown and the save-back path can never drift apart. Labels (not raw codes)
# are what the user sees.
_PRESET_VALUES: list[str] = [value for value, _label in DlssPreset.CHOICES]
_PRESET_LABELS: list[str] = [label for _value, label in DlssPreset.CHOICES]


def _clear(box: Gtk.Box) -> None:
    """Remove all children of a box (the clear-then-rebuild idiom)."""
    while (ch := box.get_first_child()) is not None:
        box.remove(ch)


def _start_download(uc: "UseCases", bar: Gtk.ProgressBar, label: Gtk.Label,
                    on_done=None) -> None:
    """Fetch the latest Streamline release. Thin delegate to the shared
    :class:`~nvidia_gui.presentation.download_worker.StreamlineDownloader` — the
    single canonical download surface. Kept (rather than callers invoking
    ``StreamlineDownloader.start`` directly) so the Dashboard quick-action and
    any in-editor button share one seam; the worker owns the thread +
    ``GLib.idle_add`` hop. No adapter imported here — the use case drives it.
    """
    StreamlineDownloader.start(uc, bar, label, on_done=on_done)


# ===========================================================================
#  Dashboard
# ===========================================================================
class DashboardView:
    """Overview: live GPU telemetry graph + snapshot readout + quick actions."""

    def __init__(self, uc: "UseCases") -> None:
        self.uc = uc
        self._root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self._graph = StatGraph(label="GPU Utilization %")
        self._fields: dict[str, TextRow] = {}
        self._build()

    def _build(self) -> None:
        title = Gtk.Label(label="Dashboard")
        title.add_css_class("nvgui-nav-title")
        title.set_xalign(0)
        self._root.append(title)

        card, body = _card("Live Telemetry",
                           "Polled from nvidia-smi on a background thread.")
        body.append(self._graph)
        for key, label in [
            ("gpu", "GPU"), ("driver", "Driver"), ("temp", "Temperature"),
            ("util", "GPU Util"), ("power", "Power"), ("clocks", "Clocks"),
            ("mem", "Memory"), ("fan", "Fan"),
        ]:
            r = TextRow(label)
            body.append(r)
            self._fields[key] = r
        self._root.append(card)

        # quick actions — fetch Streamline from GitHub (no local SDK needed)
        actions, ab = _card("Streamline DLSS cache",
                            "Download the latest NVIDIA Streamline SDK release from "
                            "GitHub and extract the DLSS DLLs into the managed cache "
                            "(~221 MB).")
        dl = Gtk.Button(label="Download Streamline (latest)")
        dl.add_css_class("nvgui-btn-primary")
        dl.connect("clicked", self._on_download)
        ab.append(dl)
        self._dl_bar = Gtk.ProgressBar()
        ab.append(self._dl_bar)
        self._dl_label = Gtk.Label(label="")
        self._dl_label.add_css_class("nvgui-card-subtle")
        self._dl_label.set_xalign(0)
        self._dl_label.set_wrap(True)
        ab.append(self._dl_label)
        self._root.append(actions)

    def _on_download(self, _b) -> None:
        _start_download(self.uc, self._dl_bar, self._dl_label)

    def root(self) -> Gtk.Widget:
        return _scrolled(self._root)

    def update(self, snap) -> None:
        """Called from the main loop with a fresh GpuSnapshot."""
        self._graph.push(_fnum(snap.gpu_util_pct))
        self._fields["gpu"].set_value(
            f"{snap.gpu_name}" if snap.gpu_name else "—")
        self._fields["driver"].set_value(
            f"{snap.driver_version} (open module)" if snap.driver_version else "—")
        self._fields["temp"].set_value(
            (snap.temperature_c + " °C") if snap.temperature_c else "—",
        )
        self._fields["util"].set_value(
            (snap.gpu_util_pct + " %") if snap.gpu_util_pct else "—")
        self._fields["power"].set_value(
            f"{snap.power_draw_w} / {snap.power_limit_w} W")
        self._fields["clocks"].set_value(
            f"{snap.gr_clock_mhz} / {snap.mem_clock_mhz} MHz")
        self._fields["mem"].set_value(
            f"{snap.mem_used_mb} / {snap.mem_total_mb} MiB")
        self._fields["fan"].set_value(
            (snap.fan_pct + " %") if snap.fan_pct else "n/a (auto)")


# ===========================================================================
#  Games (master + detail editor)
# ===========================================================================
class GamesView:
    def __init__(self, uc: "UseCases") -> None:
        self.uc = uc
        self._root = Gtk.Box()
        self._games: list[Game] = []
        self._sel: Game | None = None
        # Detected per-game capability, cached once per selected game and reused
        # across rebuilds (override flip / save / optimize / revert). Re-detected
        # only when the user switches games or hits the "Detect features" button.
        self._cap: GameCapability | None = None
        self._listbox = Gtk.ListBox()
        self._listbox.set_vexpand(True)
        self._detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._build()

    def _build(self) -> None:
        # A real Gtk.Paned replaces the old homogeneous Gtk.Box: the divider is
        # draggable and there is NO 280px hard floor - the list's minimum is its
        # content's natural size, so a narrow/tiled window no longer blocks the
        # resize. Paned draws its own handle, so the manual Gtk.Separator is gone.
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_shrink_start_child(False)
        self._paned.set_shrink_end_child(False)
        self._list_deb = Debouncer(400)
        # left: list
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        t = Gtk.Label(label="Games"); t.add_css_class("nvgui-nav-title"); t.set_xalign(0)
        t.set_hexpand(True)
        scan = Gtk.Button(label="Scan")
        scan.add_css_class("nvgui-btn-primary")
        scan.connect("clicked", lambda _b: self.refresh())
        hdr.append(t); hdr.append(scan)
        left.append(hdr)
        sw = Gtk.ScrolledWindow(); sw.set_vexpand(True); sw.set_child(self._listbox)
        left.append(sw)
        # right: detail
        rsw = Gtk.ScrolledWindow(); rsw.set_vexpand(True)
        rpad = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        rpad.set_margin_start(20); rpad.set_margin_end(20); rpad.set_margin_top(20)
        rpad.append(self._detail)
        rsw.set_child(rpad)
        self._paned.set_start_child(left)
        self._paned.set_end_child(rsw)
        self._paned.set_position(self.uc.setting("games.list_width", 300))
        self._paned.connect("notify::position", self._on_list_paned_position)
        self._root = self._paned
        self._listbox.connect("row-selected", self._on_select)
        self._show_empty_state()

    def _on_list_paned_position(self, _paned, _pspec) -> None:
        pos = max(180, min(480, self._paned.get_position()))
        self._list_deb.schedule(lambda: self.uc.set_setting("games.list_width", pos))

    def _show_empty_state(self) -> None:
        self._clear_detail()
        hint = Gtk.Label(label="Select a game to edit its per-game profile.")
        hint.add_css_class("nvgui-muted")
        self._detail.append(hint)

    def _clear_detail(self) -> None:
        _clear(self._detail)

    # ---- per-game feature detection + capability gating -------------------
    def _build_detected_card(self, game: Game) -> None:
        """The 'Detected feature support' card: a Detect button (re-runs the
        probe off-thread) + per-feature source pills from the cached capability.
        Sits above the tuning cards so the user sees what the toggles below are
        gated against."""
        c, b = _card(
            "Detected feature support",
            "Per-game NVIDIA capability probe. Unsupported features are dimmed "
            "below; click 'unsupported - override' to force-enable a feature "
            "the probe couldn't confirm.",
        )
        detrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        detb = Gtk.Button(label="Detect features")
        detb.add_css_class("nvgui-btn-ghost")
        detb.connect("clicked", self._on_detect)
        self._det_label = Gtk.Label(label="")
        self._det_label.add_css_class("nvgui-muted")
        self._det_label.set_hexpand(True)
        self._det_label.set_xalign(0)
        detrow.append(detb)
        detrow.append(self._det_label)
        b.append(detrow)
        self._det_chips = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._render_feature_chips()
        b.append(self._det_chips)
        self._detail.append(c)

    def _render_feature_chips(self) -> None:
        """Pill per feature: supported / unsupported / unknown, with source.

        UNKNOWN is NOT "unsupported" -- the probe found nothing for this kind,
        not a decisive negative. We render an "unknown" pill so the user sees
        "we're not sure" (and the gate below leaves that control open) instead
        of a false "no". Known-True lights; known-False dims (decisive);
        unknown dims too but its LABEL + gate differ from a real "unsupported".
        Notes line below if present.
        """
        _clear(self._det_chips)
        cap = self._cap
        if cap is None:
            return
        for kind, name in _FEATURE_KINDS:
            flag = getattr(cap, kind)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            if flag.source == FeatureSource.UNKNOWN:
                # uncertainty -- no source provenance to claim
                row.append(pill(f"{name}: unknown", on=False))
            else:
                state = "supported" if flag.supported else "unsupported"
                src = _feature_source_label(flag)
                row.append(pill(f"{name}: {state} ({src})", on=flag.supported))
            row.set_halign(Gtk.Align.START)
            self._det_chips.append(row)
        if cap.notes:
            n = Gtk.Label(label=cap.notes, xalign=0, wrap=True)
            n.add_css_class("nvgui-card-subtle")
            self._det_chips.append(n)

    def _gate(self, game: Game, kind: str, controls: list, body: Gtk.Box) -> None:
        """Apply capability gating to one feature's control(s) -- 3-way honest:

        - user override present   -> normal control + 'override (clear)' chip
        - detected supported       -> normal control (source shown in the card)
        - KNOWN unsupported        -> desensitized + dimmed + 'unsupported -
          override' chip (click sets an override, then refresh).
        - UNKNOWN (probe found     -> control LEFT SENSITIVE (uncertainty is not
          nothing) + a 'not yet detected' hint. We do NOT push an override chip
          for an unknown feature -- the user would be force-enabling against a
          false negative, the exact lie the detection batch exists to kill.
        """
        override = self.uc.feature_override(game, kind)
        if override is not None:
            for ctl in controls:
                ctl.set_sensitive(True)
                if hasattr(ctl, "remove_css_class"):
                    ctl.remove_css_class("nvgui-toggle-unsupported")
            body.append(self._override_chip(game, kind, clear=True))
        elif _feature_supported(self._cap, kind):
            for ctl in controls:
                ctl.set_sensitive(True)
                if hasattr(ctl, "remove_css_class"):
                    ctl.remove_css_class("nvgui-toggle-unsupported")
        elif self._is_unknown(kind):
            for ctl in controls:
                ctl.set_sensitive(True)
                if hasattr(ctl, "remove_css_class"):
                    ctl.remove_css_class("nvgui-toggle-unsupported")
            body.append(self._hint_chip("not yet detected"))
        else:
            for ctl in controls:
                ctl.set_sensitive(False)
                ctl.add_css_class("nvgui-toggle-unsupported")
            body.append(self._override_chip(game, kind, clear=False))

    def _is_unknown(self, kind: str) -> bool:
        """True iff this kind's capability is the UNKNOWN source (probe found nothing)."""
        cap = self._cap
        if cap is None:
            return False
        return getattr(cap, kind).source == FeatureSource.UNKNOWN

    def _hint_chip(self, text: str) -> Gtk.Label:
        """A non-actionable hint (e.g. 'not yet detected') -- never an override push."""
        h = Gtk.Label(label=text)
        h.add_css_class("nvgui-card-subtle")
        h.set_halign(Gtk.Align.START)
        return h

    def _override_chip(self, game: Game, kind: str, *, clear: bool) -> Gtk.Button:
        if clear:
            btn = Gtk.Button(label="override (clear)")
            btn.connect("clicked", lambda _b: self._apply_override(game, kind, clear=True))
        else:
            btn = Gtk.Button(label="unsupported - override")
            btn.connect("clicked", lambda _b: self._apply_override(game, kind, clear=False))
        btn.add_css_class("nvgui-feature-chip")
        btn.set_halign(Gtk.Align.START)
        return btn

    def _apply_override(self, game: Game, kind: str, *, clear: bool) -> None:
        try:
            if clear:
                self.uc.clear_feature_override(game, kind)
            else:
                self.uc.set_feature_override(game, kind, True)
        except Exception as exc:  # noqa: BLE001 -- settings.set may raise (disk full / perms)
            # Surface the persist failure honestly -- without this the chip
            # wouldn't refresh and the user would see nothing, concluding the
            # click didn't register while the override silently never saved.
            logger.warning("feature override persist failed: %s", exc)
            self._det_label.set_text(f"override save failed - {exc}")
            return
        # Reuse the cached capability (override changes gating only, not the
        # probe result) — a rebuild re-reads feature_override and re-gates.
        if self._sel is not None:
            self._build_editor(self._sel)

    def _on_detect(self, _b) -> None:
        if self._sel is None:
            return
        self._det_label.set_text("Detecting…")
        sel = self._sel

        def work() -> None:
            try:
                cap = self.uc.detect_game_features(sel)
                GLib.idle_add(lambda c=cap: self._after_detect(c))
            except Exception as exc:  # noqa: BLE001
                logger.warning("detect failed: %s", exc)
                GLib.idle_add(
                    lambda e=exc: self._det_label.set_text(f"detect failed: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _after_detect(self, cap: GameCapability) -> bool:
        self._cap = cap
        self._det_label.set_text("")
        if self._sel is not None:
            self._build_editor(self._sel)
        return False

    def root(self) -> Gtk.Widget:
        return self._root

    def refresh(self) -> None:
        self._games = self.uc.scan_games()
        self._listbox.remove_all()
        # Stash each row's index on its child box: GTK4 dropped the
        # `Gtk.ListBoxRow.get_index()` API that GTK3 had, so recovering the
        # index from a selected row needs us to carry it ourselves.
        for i, g in enumerate(self._games):
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row.set_margin_start(10); row.set_margin_end(10)
            row.set_margin_top(8); row.set_margin_bottom(8)
            nm = Gtk.Label(label=g.name, xalign=0); nm.add_css_class("nvgui-row-label")
            meta = Gtk.Label(label=f"{g.appid} · {g.installdir}",
                             xalign=0)
            meta.add_css_class("nvgui-card-subtle")
            row.append(nm); row.append(meta)
            row._nvgui_index = i
            self._listbox.append(row)
        # default selection — fires _on_select with row 0, building its editor
        if self._games:
            self._listbox.select_row(self._listbox.get_row_at_index(0))
        else:
            self._show_empty_state()

    def _on_select(self, _lb, row) -> None:
        # row is the ListBoxRow the listbox wraps around our child box; the
        # index lives on that child (see refresh) — GTK4 has no row->index API.
        if row is None:
            self._show_empty_state()
            return
        child = row.get_child()
        idx = getattr(child, "_nvgui_index", -1) if child is not None else -1
        if idx < 0 or idx >= len(self._games):
            self._show_empty_state()
            return
        self._sel = self._games[idx]
        self._build_editor(self._sel)

    def _build_editor(self, game: Game) -> None:
        self._clear_detail()
        p = self.uc.get_profile(game.appid)
        title = Gtk.Label(label=game.name); title.add_css_class("nvgui-card-title")
        title.set_xalign(0); self._detail.append(title)
        sub = Gtk.Label(label=f"{game.appid} · {game.installdir}")
        sub.add_css_class("nvgui-card-subtle"); sub.set_xalign(0)
        self._detail.append(sub)

        # widgets consumed on save
        self._ed: dict = {}
        self._extra_env_rows: list[tuple[Gtk.Entry, Gtk.Entry]] = []

        def toggle(key, label, subtitle, val):
            tr = ToggleRow(label, subtitle, val)
            self._ed[key] = tr
            return tr

        # ---- Detected feature support (capability, read-only) ----
        # Cache one GameCapability per selected game; reuse across rebuilds so
        # an override flip or save doesn't re-hit the (possibly online) probe.
        # The probe is offline-graceful; defensively treat a raising stub as
        # all-UNKNOWN so the editor never crashes mid-detection.
        if getattr(self, "_cap", None) is None or self._cap.appid != game.appid:
            try:
                self._cap = self.uc.detect_game_features(game)
            except Exception as exc:  # noqa: BLE001
                logger.debug("feature detect failed on open: %s", exc)
                self._cap = GameCapability(appid=game.appid)
        self._build_detected_card(game)

        # ---- GPU & Ray Tracing ----
        # the first-class knobs that decide whether the Proton game can SEE the
        # card and use RT/DXR. Subtitles show the REAL emitted var, not a ghost.
        c, b = _card("GPU & Ray Tracing",
                     "Whether the Proton game can see this NVIDIA card and use RT/DXR.")
        b.append(toggle("expose_gpu", "Expose NVIDIA GPU",
                        "PROTON_HIDE_NVIDIA_GPU=0 · makes the Proton game see the card",
                        p.expose_nvidia_gpu))
        b.append(toggle("rtx", "Enable Ray Tracing",
                        "Needs NVAPI on — the gate for RT/DXR content",
                        p.enable_rtx))
        b.append(toggle("dxr", "Force DXR",
                        "VKD3D_CONFIG=dxr · the real force token "
                        "(dxr11 is silently ignored by modern vkd3d-proton)",
                        p.force_dxr))
        # RT/DXR is game-specific: gate both switches by the 'rt' capability.
        # expose_gpu above is deliberately NOT gated (every Proton game can
        # hide/expose the GPU regardless of RT support).
        self._gate(game, "rt", [self._ed["rtx"], self._ed["dxr"]], b)
        self._detail.append(c)

        # ---- NVIDIA NVAPI ----
        c, b = _card("NVIDIA NVAPI",
                     "NVAPI plumbing so DLSS/Reflex can talk to the driver.")
        b.append(toggle("nvapi", "Proton NVAPI", "PROTON_ENABLE_NVAPI=1", p.enable_nvapi))
        b.append(toggle("dxvk_nvapi", "DXVK NVAPI",
                        "DXVK_ENABLE_NVAPI=1 · disables DXVK's nvapiHack",
                        p.enable_dxvk_nvapi))
        self._detail.append(c)

        # ---- DLSS ----
        c, b = _card("DLSS",
                     "DLSS-SR preset + Frame Generation, via the dxvk-nvapi DRS overrides.")
        plbl = Gtk.Label(label="DLSS-SR preset · "
                              "DXVK_NVAPI_DRS_NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION")
        plbl.add_css_class("nvgui-card-subtle"); plbl.set_xalign(0); plbl.set_wrap(True)
        b.append(plbl)
        dropdown = Gtk.DropDown.new_from_strings(_PRESET_LABELS)
        cur = p.dlss_preset or DlssPreset.DISABLED
        try:
            dropdown.set_selected(_PRESET_VALUES.index(cur))
        except ValueError:
            dropdown.set_selected(0)
        self._ed["preset"] = dropdown
        b.append(dropdown)
        # DLSS-SR is game-specific: gate the preset dropdown by the dlss_sr
        # capability (only the dropdown — FG below has its own dlss_fg gate).
        self._gate(game, "dlss_sr", [self._ed["preset"]], b)
        b.append(toggle("fg", "DLSS-G Frame Generation",
                        "DXVK_NVAPI_DRS_NGX_DLSS_FG_OVERRIDE=on", p.enable_dlss_fg))
        # Frame Generation is game-specific: gate by the dlss_fg capability.
        self._gate(game, "dlss_fg", [self._ed["fg"]], b)
        self._detail.append(c)

        # ---- Latency & scheduling ----
        c, b = _card("Latency & scheduling",
                     "Reflex frame pacing + scheduling/overlay helpers.")
        b.append(toggle("reflex", "Vulkan Reflex",
                        "DXVK_NVAPI_VKREFLEX=1 · low-latency frame pacing",
                        p.enable_reflex))
        # Reflex is game-specific: gate by the reflex capability. GameMode and
        # MangoHud are broadly available and deliberately NOT gated.
        self._gate(game, "reflex", [self._ed["reflex"]], b)
        b.append(toggle("gamemode", "Feral GameMode", "GAMEMODERUN=1", p.enable_gamemode))
        b.append(toggle("mangohud", "MangoHud overlay", "MANGOHUD=1", p.enable_mangohud))
        self._detail.append(c)

        # ---- Custom environment (advanced) ----
        # escape hatch for vars the curated toggles don't surface. extra_env
        # wins over the toggles for the same var (and VKD3D_CONFIG is merged).
        c, b = _card("Custom environment (advanced)",
                     "Escape hatch: extra KEY=VALUE the toggles above don't surface. "
                     "Wins over the toggles for the same variable (VKD3D_CONFIG "
                     "tokens are merged, not overwritten).",
                     )
        self._env_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for k, v in sorted((p.extra_env or {}).items()):
            self._env_append_row(k, v)
        b.append(self._env_box)
        add_btn = Gtk.Button(label="+ Add env var"); add_btn.add_css_class("nvgui-btn-ghost")
        add_btn.connect("clicked", lambda _w: self._env_append_row("", ""))
        b.append(add_btn)
        self._detail.append(c)

        # ---- DLSS DLL swap ----
        c, b = _card("DLSS DLL swap",
                     "Opt-in: replace the game's nvngx_dlss.dll with a cached version.")
        sw_toggle = ToggleRow("Enable DLL swap", "Backs up the original first.",
                              p.dlss_dll_swap)
        self._ed["swap"] = sw_toggle
        b.append(sw_toggle)
        # version dropdown (populated on refresh)
        self._swap_dd = Gtk.DropDown.new_from_strings(["(no versions cached)"])
        self._ed["swap_ver"] = None
        b.append(self._swap_dd)
        # swap status + revert buttons
        st_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._swap_status = st_box
        b.append(st_box)
        btnrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        applyb = Gtk.Button(label="Apply swap"); applyb.add_css_class("nvgui-btn-primary")
        revertb = Gtk.Button(label="Revert swap"); revertb.add_css_class("nvgui-btn-danger")
        applyb.connect("clicked", self._on_apply_swap)
        revertb.connect("clicked", self._on_revert_swap)
        btnrow.append(applyb); btnrow.append(revertb)
        b.append(btnrow)
        self._detail.append(c)
        self._refresh_swap_versions()

        # ---- Diagnostics ----
        c, b = _card("Diagnostics",
                     "Read-only probe of this game's launch environment: Vulkan ICD/RT, "
                     "the mapped Proton tool, prefix NVAPI/DLSS DLLs, prefix age.")
        diagrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        runb = Gtk.Button(label="Run diagnostics"); runb.add_css_class("nvgui-btn-primary")
        runb.connect("clicked", self._on_run_diag)
        self._diag_label = Gtk.Label(label=""); self._diag_label.add_css_class("nvgui-muted")
        self._diag_label.set_hexpand(True)
        diagrow.append(runb); diagrow.append(self._diag_label)
        b.append(diagrow)
        self._diag_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        b.append(self._diag_box)
        self._detail.append(c)

        # ---- Save bar + Optimize for this rig ----
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        save = Gtk.Button(label="Save profile"); save.add_css_class("nvgui-btn-primary")
        save.connect("clicked", self._on_save)
        opt = Gtk.Button(label="Optimize for this rig"); opt.add_css_class("nvgui-btn-primary")
        opt.set_tooltip_text(
            "Read detected hardware and set the recommended RT/DLSS/latency "
            "toggles for this game (preserves DLL swap + custom env).")
        opt.connect("clicked", self._on_optimize)
        reset = Gtk.Button(label="Revert to saved"); reset.add_css_class("nvgui-btn-ghost")
        reset.connect("clicked", lambda _b: self._build_editor(self._sel))
        bar.append(save); bar.append(opt); bar.append(reset)
        self._status_label = Gtk.Label(label=""); self._status_label.add_css_class("nvgui-muted")
        self._status_label.set_hexpand(True)
        bar.append(self._status_label)
        self._detail.append(bar)

    # ---- custom env editor rows ------------------------------------------
    def _env_append_row(self, key: str, value: str) -> None:
        """A KEY / value / remove row, appended to the editor's env box."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        k = Gtk.Entry(placeholder_text="KEY"); k.set_hexpand(True); k.set_text(key or "")
        v = Gtk.Entry(placeholder_text="value"); v.set_hexpand(True); v.set_text(value or "")
        rm = Gtk.Button(label="–"); rm.add_css_class("nvgui-btn-ghost")
        pair = (k, v)
        self._extra_env_rows.append(pair)

        def remove_row(_b) -> None:
            self._env_box.remove(row)
            if pair in self._extra_env_rows:
                self._extra_env_rows.remove(pair)
        rm.connect("clicked", remove_row)
        row.append(k); row.append(v); row.append(rm)
        self._env_box.append(row)

    def _collect_extra_env(self) -> dict:
        out: dict[str, str] = {}
        for k_e, v_e in self._extra_env_rows:
            k = k_e.get_text().strip()
            if not k:
                continue  # blank-key rows carry no env and aren't saved
            out[k] = v_e.get_text()
        return out

    # ---- Optimize for this rig (NVIDIA-App-style) ------------------------
    def _on_optimize(self, _b) -> None:
        if self._sel is None:
            return
        self._status_label.set_text("Optimizing for this rig…")
        sel = self._sel

        def work() -> None:
            try:
                res = self.uc.optimize_for_rig(sel)
                GLib.idle_add(lambda r=res: self._after_optimize(r))
            except Exception as exc:  # noqa: BLE001
                logger.warning("optimize failed: %s", exc)
                GLib.idle_add(lambda e=exc: (self._status_label.set_text(f"optimize: {e}")))

        threading.Thread(target=work, daemon=True).start()

    def _after_optimize(self, res) -> bool:
        self._status_label.set_text(res.message if res.ok else f"optimize: {res.message}")
        # rebuild from the now-persisted profile so the applied toggles are visible
        if self._sel is not None:
            self._build_editor(self._sel)
        return False

    # ---- Diagnostics card (async probe → main-loop render) ---------------
    def _on_run_diag(self, _b) -> None:
        if self._sel is None:
            return
        self._diag_label.set_text("Probing…")
        _clear(self._diag_box)
        sel = self._sel

        def work() -> None:
            try:
                rep = self.uc.run_diagnostics(sel)
                GLib.idle_add(lambda r=rep: self._render_diag(r))
            except Exception as exc:  # noqa: BLE001
                logger.warning("diagnostics failed: %s", exc)
                GLib.idle_add(lambda e=exc: self._diag_label.set_text(f"failed: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _render_diag(self, rep) -> bool:
        self._diag_label.set_text("")
        _clear(self._diag_box)
        if rep.verdict:
            v = Gtk.Label(label=rep.verdict); v.set_xalign(0); v.set_wrap(True)
            v.add_css_class("nvgui-card-subtle")
            self._diag_box.append(v)
        for chk in rep.checks:
            on = chk.status in (DiagStatus.OK, DiagStatus.INFO)
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            line.append(pill(chk.status.upper(), on=on))
            txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            nm = Gtk.Label(label=chk.name); nm.set_xalign(0); nm.add_css_class("nvgui-row-label")
            txt.append(nm)
            if chk.detail:
                d = Gtk.Label(label=chk.detail); d.set_xalign(0); d.set_wrap(True)
                d.add_css_class("nvgui-card-subtle"); txt.append(d)
            if chk.fix_hint:
                f = Gtk.Label(label="→ " + chk.fix_hint); f.set_xalign(0); f.set_wrap(True)
                f.add_css_class("nvgui-muted"); txt.append(f)
            line.append(txt)
            self._diag_box.append(line)
        return False

    # ---- flush pending paned persistence (main-loop teardown) ------------
    def flush(self) -> None:
        """Flush any pending list-divider persist so the last drag survives close."""
        if getattr(self, "_list_deb", None) is not None:
            self._list_deb.flush()

    def _refresh_swap_versions(self) -> None:
        if self._sel is None or not hasattr(self, "_swap_dd"):
            return
        versions = [v.version for v in self.uc.list_dlss_versions()]
        self._ed["versions"] = versions
        _clear(self._swap_status)
        if not versions:
            hint = Gtk.Label(label="No cached versions yet. Use the DLSS page "
                                   "(or the Dashboard quick action) to fetch "
                                   "Streamline, then a version will be seeded here.")
            hint.add_css_class("nvgui-muted"); hint.set_xalign(0)
            self._swap_status.append(hint)
            return
        try:
            status = self.uc.dlss_swap_status(self._sel, None)
            for kind, st in status.items():
                self._swap_status.append(
                    TextRow(f"{kind}", st, accent=(st == "backed-up")))
        except Exception as exc:  # noqa: BLE001
            logger.debug("swap status skipped: %s", exc)

    def _on_apply_swap(self, _b) -> None:
        if self._sel is None:
            return
        if not self._ed["swap"].active:
            self._status_label.set_text("Enable the DLL swap toggle first.")
            return
        vers = self._ed.get("versions") or []
        if not vers:
            self._status_label.set_text("Seed a DLSS version first.")
            return
        # default to first/only cached version
        ver = vers[0]
        try:
            res = self.uc.apply_dlss_swap(self._sel, ver, None)
        except Exception as exc:  # noqa: BLE001 -- a mid-copy I/O failure must surface
            # The adapter may raise mid-copy (EROFS / ENOSPC / perms) and leave a
            # half-applied backup on disk; the DLSS page wraps this too -- the
            # status must NOT silently stay stale, or the swap looks absent
            # while the DLL partially landed.
            logger.warning("apply_dlss_swap raised: %s", exc)
            self._status_label.set_text(f"swap failed - {exc}")
            self._refresh_swap_versions()
            return
        self._status_label.set_text(
            res.message if res.ok else f"swap: {res.message}")
        self._refresh_swap_versions()

    def _on_revert_swap(self, _b) -> None:
        if self._sel is None:
            return
        try:
            res = self.uc.revert_dlss_swap(self._sel, None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("revert_dlss_swap raised: %s", exc)
            self._status_label.set_text(f"revert failed - {exc}")
            self._refresh_swap_versions()
            return
        self._status_label.set_text(
            res.message if res.ok else f"revert: {res.message}")
        self._refresh_swap_versions()

    def _on_save(self, _b) -> None:
        if self._sel is None:
            return
        from ..domain.models import GameProfile
        p = GameProfile(appid=self._sel.appid)
        # GPU & RT
        p.expose_nvidia_gpu = self._ed["expose_gpu"].active
        p.enable_rtx = self._ed["rtx"].active
        p.force_dxr = self._ed["dxr"].active
        # NVAPI
        p.enable_nvapi = self._ed["nvapi"].active
        p.enable_dxvk_nvapi = self._ed["dxvk_nvapi"].active
        # DLSS
        idx = self._ed["preset"].get_selected()
        p.dlss_preset = (_PRESET_VALUES[idx]
                         if 0 <= idx < len(_PRESET_VALUES) else DlssPreset.DISABLED)
        p.enable_dlss_fg = self._ed["fg"].active
        # Latency & scheduling
        p.enable_reflex = self._ed["reflex"].active
        p.enable_gamemode = self._ed["gamemode"].active
        p.enable_mangohud = self._ed["mangohud"].active
        # DLL swap
        p.dlss_dll_swap = self._ed["swap"].active
        vers = self._ed.get("versions") or []
        p.dlss_swap_version = vers[0] if (p.dlss_dll_swap and vers) else ""
        # custom env escape hatch (collected from the editable rows)
        p.extra_env = self._collect_extra_env()
        res = self.uc.save_profile(p)
        self._status_label.set_text(res.message if res.ok else f"save: {res.message}")


# ===========================================================================
#  Graphics (global driver: ReBAR + NVreg registry)
# ===========================================================================
class GraphicsView:
    def __init__(self, uc: "UseCases") -> None:
        self.uc = uc
        self._root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self._rebar = None
        self._regdw = None
        self._regdw_pd = None
        self._status = None
        self._persist = None
        self._persist_status = None
        self._build()

    def _build(self) -> None:
        title = Gtk.Label(label="Graphics"); title.add_css_class("nvgui-nav-title")
        title.set_xalign(0); self._root.append(title)

        c, b = _card("Global driver settings",
                     "Written to /etc/modprobe.d/nvidia-gui.conf. Applied at next boot "
                     "(NVreg module params are load-time on the open kernel module). "
                     "Uses a polkit/sudo prompt to write with root.")
        self._rebar = ToggleRow("Resizable BAR (ReBAR)",
                                "NVreg_EnableResizableBar=1 — improves GPU-CPU memory "
                                "access for supporting systems.", False)
        b.append(self._rebar)
        b.append(Gtk.Separator())
        lab = Gtk.Label(label="NVreg_RegistryDwords",
                        xalign=0); lab.add_css_class("nvgui-row-label")
        b.append(lab)
        hint = Gtk.Label(label="e.g.  PerfLevelSrc=0x3322;PowerMizerEnable=1;",
                         xalign=0); hint.add_css_class("nvgui-card-subtle")
        b.append(hint)
        self._regdw = Gtk.Entry(); self._regdw.set_placeholder_text("PerfLevelSrc=0x3322;PowerMizerEnable=1;")
        b.append(self._regdw)
        lab2 = Gtk.Label(label="NVreg_RegistryDwordsPerDevice", xalign=0)
        lab2.add_css_class("nvgui-row-label"); b.append(lab2)
        self._regdw_pd = Gtk.Entry(); self._regdw_pd.set_placeholder_text("(optional)")
        b.append(self._regdw_pd)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        applyb = Gtk.Button(label="Apply (root)"); applyb.add_css_class("nvgui-btn-primary")
        applyb.connect("clicked", self._on_apply)
        reloadb = Gtk.Button(label="Reload current"); reloadb.add_css_class("nvgui-btn-ghost")
        reloadb.connect("clicked", lambda _b: self.refresh())
        self._status = Gtk.Label(label=""); self._status.add_css_class("nvgui-muted")
        self._status.set_hexpand(True); self._status.set_wrap(True)
        bar.append(applyb); bar.append(reloadb); bar.append(self._status)
        b.append(bar)
        self._root.append(c)

        self._build_persistence()
        self._build_reference()
        self.refresh()

    def _build_reference(self) -> None:
        """A read-only crib of what every managed NVreg key does — so the raw
        ``NVreg_RegistryDwords`` entry box isn't a finger-in-the-air guess. The
        full prose reference is ``docs/registry-dwords.md`` in the repo."""
        ref, rb = _card("Registry dword reference",
                        "Curated keys + the always-managed lines. "
                        "Full reference: docs/registry-dwords.md in the repo.")
        for key, eff in [
            ("PerfLevelSrc",
             "Hex selecting the clock/perf-level source PowerMizer consults."),
            ("PowerMizerEnable",
             "1 enables the adaptive PowerMizer clock governor, 0 disables it."),
            ("NVreg_EnableResizableBar",
             "The toggle above; opens the PCI ReBAR aperture (load-time)."),
            ("nvidia_drm modeset",
             "Managed by the app skeleton (always 1); required for Wayland/VRR."),
        ]:
            rb.append(TextRow(key, eff))
        self._root.append(ref)

    def _on_apply(self, _b) -> None:
        from ..domain.models import GlobalDriverSettings
        s = GlobalDriverSettings(
            enable_resizable_bar=self._rebar.active,
            registry_dwords=self._regdw.get_text().strip(),
            registry_dwords_per_device=self._regdw_pd.get_text().strip(),
        )
        ok, msg = self.uc.apply_global_settings(s)
        self._status.set_text(msg)
        self._status.remove_css_class("nvgui-muted")
        if ok:
            self._status.add_css_class("nvgui-row-value"); self._status.add_css_class("accent")
        else:
            self._status.add_css_class("nvgui-btn-danger")

    def _build_persistence(self) -> None:
        """Persistence Mode card. Distinct from the modprobe (boot-time) knobs
        above: this manages the *live* nvidia-persistenced service, so it takes
        effect immediately AND survives reboot — the fix for tab-in stutter
        that the modprobe dwords cannot address (those are load-time only)."""
        c, b = _card("Persistence Mode",
                     "Keeps the NVIDIA driver loaded between accesses so the GPU "
                     "doesn't re-initialise on focus change — the tab-in stutter "
                     "you see returning to a game. Manages the system "
                     "nvidia-persistenced service, so it SURVIVES reboot (unlike "
                     "`nvidia-smi -pm 1`). Takes effect immediately — no reboot. "
                     "Uses a polkit/sudo prompt.")
        self._persist = ToggleRow("Persistence Mode (survives reboot)",
                                  "Enable nvidia-persistenced — recommended for "
                                  "gaming on NVIDIA-open.", False)
        b.append(self._persist)
        self._persist_status = Gtk.Label(label="")
        self._persist_status.set_xalign(0)
        self._persist_status.set_hexpand(True)
        self._persist_status.set_wrap(True)
        self._persist_status.add_css_class("nvgui-muted")
        b.append(self._persist_status)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        applyb = Gtk.Button(label="Apply (root)")
        applyb.add_css_class("nvgui-btn-primary")
        applyb.connect("clicked", self._on_persist_apply)
        bar.append(applyb)
        b.append(bar)
        self._root.append(c)

    def _on_persist_apply(self, _b) -> None:
        ok, msg = self.uc.set_persistence(self._persist.active)
        self._persist_status.set_text(msg)
        self._persist_status.remove_css_class("nvgui-muted")
        self._persist_status.remove_css_class("nvgui-btn-danger")
        if ok:
            self._persist_status.add_css_class("nvgui-row-value")
        else:
            self._persist_status.add_css_class("nvgui-btn-danger")
        self._refresh_persistence()

    def _refresh_persistence(self) -> None:
        try:
            st = self.uc.persistence_status()
        except Exception:  # noqa: BLE001 — a status read must never crash the view
            return
        if not st.supported:
            self._persist.set_active(False)
            self._persist_status.set_text("nvidia-persistenced not installed on this system.")
            return
        self._persist.set_active(st.survives_reboot)
        self._persist_status.set_text(
            f"Service: {'enabled' if st.survives_reboot else 'disabled'} / "
            f"{'active' if st.active else 'inactive'}   ·   "
            f"GPU persistence: {'on' if st.live_mode else 'off'}"
        )

    def refresh(self) -> None:
        s = self.uc.get_global_settings()
        self._rebar.set_active(s.enable_resizable_bar)
        self._regdw.set_text(s.registry_dwords or "")
        self._regdw_pd.set_text(s.registry_dwords_per_device or "")
        self._refresh_persistence()

    def root(self) -> Gtk.Widget:
        return _scrolled(self._root)


# ===========================================================================
#  Display (read-only)
# ===========================================================================
class DisplayView:
    def __init__(self, uc: "UseCases") -> None:
        self.uc = uc
        self._root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self._build()

    def _build(self) -> None:
        title = Gtk.Label(label="Display"); title.add_css_class("nvgui-nav-title")
        title.set_xalign(0); self._root.append(title)

        info = self.uc.display_info()
        c, b = _card("Display server",
                     "Read-only. Mode/gamma/vibrance tuning that the Windows NVIDIA App "
                     "applies has no equivalent userspace path on Wayland.")
        b.append(TextRow("Server", info.server))
        b.append(TextRow("Monitors", ", ".join(info.monitors) or "—"))
        b.append(TextRow("VRR / GSYNC capable", "Yes" if info.vrr_capable else "No",
                         accent=info.vrr_capable))
        b.append(TextRow("Notes", info.notes))
        self._root.append(c)

    def root(self) -> Gtk.Widget:
        return _scrolled(self._root)


# ===========================================================================
#  Drivers (read-only + link to Graphics)
# ===========================================================================
class DriversView:
    def __init__(self, uc: "UseCases") -> None:
        self.uc = uc
        self._root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self._build()

    def _build(self) -> None:
        title = Gtk.Label(label="Drivers"); title.add_css_class("nvgui-nav-title")
        title.set_xalign(0); self._root.append(title)
        info = self.uc.driver_info()
        snap = self.uc.snapshot()
        c, b = _card("Driver & module")
        b.append(TextRow("Driver version", info.driver_version or snap.driver_version or "—"))
        b.append(TextRow("Module", info.module_name))
        b.append(TextRow("Module version", info.module_version or "—"))
        b.append(TextRow("Branch", info.branch or "—",
                         accent=(info.branch == "open")))
        b.append(TextRow("GPU", snap.gpu_name or "—"))
        b.append(TextRow("nvidia-settings writes",
                         "Inert (NV-CONTROL needs X)" if info.compositor_incompatible
                         else "available"))
        b.append(TextRow("modprobe.d config", info.modprobe_config_path or "—"))
        self._root.append(c)

        # read-only cache summary — the download manager lives on the DLSS
        # page now (single canonical Streamline surface). Listing the cached
        # versions here is still useful as a glance at which swaps are seeded.
        c2, b2 = _card("DLSS cache")
        versions = self.uc.list_dlss_versions()
        if versions:
            for v in versions:
                b2.append(TextRow(v.version, pathlib.Path(v.path).parent.name))
        else:
            b2.append(Gtk.Label(label="No versions cached. Manage the cache "
                                      "from the DLSS page.", xalign=0))
            b2.get_last_child().add_css_class("nvgui-muted")
        self._root.append(c2)

    def refresh(self) -> None:
        _clear(self._root)
        self._build()

    def root(self) -> Gtk.Widget:
        return _scrolled(self._root)


# ===========================================================================
#  RTX (read-only RTX summary + honest feature parity - no download)
# ===========================================================================
class RtxView:
    def __init__(self, uc: "UseCases") -> None:
        self.uc = uc
        self._root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self._build()

    def _build(self) -> None:
        title = Gtk.Label(label="RTX"); title.add_css_class("nvgui-nav-title")
        title.set_xalign(0); self._root.append(title)

        # Read-only glance at the managed cache — the download manager now
        # lives on the DLSS page (single canonical Streamline surface). Kept
        # here so the RTX page still shows which swap versions are seeded.
        c, b = _card("Cached DLSS versions",
                     "The managed swap cache. Fetch and manage versions on the "
                     "DLSS page — this is a read-only glance.")
        versions = self.uc.list_dlss_versions()
        if versions:
            for v in versions:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                nm = Gtk.Label(label=v.version); nm.add_css_class("nvgui-row-label")
                nm.set_hexpand(True)
                row.append(nm)
                row.append(pill("cached"))
                b.append(row)
        else:
            hint = Gtk.Label(label="No cached versions. Open the DLSS page to "
                                   "fetch the latest Streamline release.")
            hint.add_css_class("nvgui-muted"); hint.set_xalign(0); hint.set_wrap(True)
            b.append(hint)
        self._root.append(c)

        c2, b2 = _card("Honest feature parity",
                       "These Windows-App RTX features have no Linux userspace equivalent "
                       "and are intentionally not offered:")
        for feat, note in [
            ("Freestyle / Game Filter", "inline shader LUT — no Linux hook; greyed honestly."),
            ("Smooth Motion", "frame interpolation driver-side; no Linux API."),
            ("G-Assist / NVIDIA AI", "cloud assistant; not local on Linux."),
            ("RTX HDR", "inline HDR tonemap; no Linux API."),
        ]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            n = Gtk.Label(label=feat); n.add_css_class("nvgui-row-label"); n.set_hexpand(True)
            row.append(n)
            row.append(pill("not available", on=False))
            b2.append(row)
            desc = Gtk.Label(label=note, xalign=0, wrap=True)
            desc.add_css_class("nvgui-card-subtle"); b2.append(desc)
        self._root.append(c2)

    def root(self) -> Gtk.Widget:
        return _scrolled(self._root)


# ===========================================================================
#  Profiles (summary of saved per-game profiles)
# ===========================================================================
class ProfilesView:
    def __init__(self, uc: "UseCases") -> None:
        self.uc = uc
        self._root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self._build()

    def _build(self) -> None:
        title = Gtk.Label(label="Profiles"); title.add_css_class("nvgui-nav-title")
        title.set_xalign(0); self._root.append(title)

        gs = self.uc.get_global_settings()
        c, b = _card("Global driver profile")
        b.append(TextRow("ReBAR", "on" if gs.enable_resizable_bar else "off",
                         accent=gs.enable_resizable_bar))
        b.append(TextRow("Registry dwords", gs.registry_dwords or "(stock)"))
        b.append(TextRow("Per-device dwords", gs.registry_dwords_per_device or "(stock)"))
        self._root.append(c)

        c2, b2 = _card("Saved game profiles")
        games = self.uc.scan_games()
        any_saved = False
        for g in games:
            p = self.uc.get_profile(g.appid)
            flags = self.uc.profile_flags(p)
            if not flags and not p.extra_env:
                continue
            any_saved = True
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            row.set_margin_top(6); row.set_margin_bottom(6)
            nm = Gtk.Label(label=g.name, xalign=0); nm.add_css_class("nvgui-row-label")
            row.append(nm)
            chips = Gtk.Box(spacing=6)
            for f in flags:
                chips.append(pill(f))
            chips.set_halign(Gtk.Align.START)
            row.append(chips)
            sep = Gtk.Separator()
            b2.append(row); b2.append(sep)
        if not any_saved:
            h = Gtk.Label(label="No per-game profiles saved yet. Open the Games page and "
                                "tune a game's launch options.")
            h.add_css_class("nvgui-muted"); h.set_wrap(True); h.set_xalign(0)
            b2.append(h)
        self._root.append(c2)

    def root(self) -> Gtk.Widget:
        return _scrolled(self._root)


# ---------------------------------------------------------------------------
def _fnum(s: str) -> float | None:
    try:
        return float(s.strip().rstrip("%")) if s and s.strip() else None
    except (TypeError, ValueError):
        return None
