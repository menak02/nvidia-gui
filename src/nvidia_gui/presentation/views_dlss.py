"""DLSS page -- the canonical Streamline cache + per-game DLL swap console.

This is the ONE place the Streamline download lives: the single
``Download Streamline (latest)`` button drives
:class:`~nvidia_gui.presentation.download_worker.StreamlineDownloader`. It
supersedes the old per-view download buttons (Dashboard/Drivers/RTX kept
their own copies; the DLSS page is now the single canonical surface).

The page also enumerates every cached DLSS/Streamline DLL version and offers
per-game swap/revert against the games discovered via the use-case facade,
plus a power-user seed-from-folder escape hatch.

Hexagonal seam: imports only ``application`` + ``domain`` + ``gi`` + sibling
presentation modules -- never an adapter. Every DLSS operation routes through
:class:`~nvidia_gui.application.use_cases.UseCases`.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from ..domain.models import DlssVersion, Game  # noqa: E402  (type helpers)
from .download_worker import StreamlineDownloader
from .widgets import TextRow, confirm_destructive, pill, toplevel_of

if TYPE_CHECKING:
    from ..application.use_cases import UseCases

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Local visual helpers -- reimplemented here (NOT imported from views) so the
#  DLSS page owns its rendering and views.py stays sole-owned by games-ui.
# ---------------------------------------------------------------------------
def _card(title: str, subtitle: str | None = None) -> tuple[Gtk.Box, Gtk.Box]:
    """Return (card, body) so callers append rows to body. Mirrors the views
    card idiom (header + optional subtitle + vertical body) without importing
    the private ``views._card``."""
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
    """Wrap a page column in a vexpand scrolled window with the app margins --
    the same envelope the class views use via ``views._scrolled``."""
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


def _clear(box: Gtk.Box) -> None:
    """Remove every child of a box (the clear-then-rebuild idiom)."""
    while (ch := box.get_first_child()) is not None:
        box.remove(ch)


def _fill_dropdown(dd: Gtk.DropDown, labels: list[str], fallback: str) -> None:
    """Set a DropDown's model to *labels*, or a single muted *fallback* when
    empty so the widget never presents a zero-item (unselectable) state."""
    dd.set_model(Gtk.StringList.new(list(labels) if labels else [fallback]))
    dd.set_selected(0)


def build_dlss_view(uc: "UseCases",
                    on_status: "Callable[[str], None] | None" = None,
                    ) -> Gtk.Widget:
    """Build the DLSS page: cached-version list, the single Streamline
    download, per-game swap/revert, and seed-from-folder.

    ``on_status`` (optional) is the canonical confirmation surface — the
    window's save toast. Swap apply/revert push their result message through it
    for transient feedback, in addition to the persistent inline status label.
    Backward-compatible: callers that pass only ``uc`` (tests, older call sites)
    get the inline-label-only behavior (on_status None → skipped).
    """
    column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)

    title = Gtk.Label(label="DLSS")
    title.add_css_class("nvgui-nav-title")
    title.set_xalign(0)
    column.append(title)

    intro = Gtk.Label(
        label="Streamline cache, the canonical download, and per-game DLSS DLL swap."
    )
    intro.add_css_class("nvgui-card-subtle")
    intro.set_xalign(0)
    intro.set_wrap(True)
    column.append(intro)

    # ----- shared, mutable state reachable from the closures ----------------
    # Both the cache list and the swap version dropdown read the SAME version
    # set, so ``state["versions"]`` is the single source the dropdown indexes
    # into (parallel to its Gtk.StringList). ``state["games"]`` is the parallel
    # list for the game dropdown.
    state: dict = {"games": [], "versions": []}

    # ===== Card 1 -- cached Streamline versions ==============================
    cache_card, cache_body = _card(
        "Streamline versions",
        "Locally cached DLSS / Streamline DLL packages available for the swap.",
    )
    column.append(cache_card)

    # ===== Card 2 -- the single Streamline download ==========================
    dl_card, dl_body = _card(
        "Download Streamline",
        "Fetch the latest NVIDIA Streamline release from GitHub and extract the "
        "known DLLs into the cache. The single canonical download for the app.",
    )
    column.append(dl_card)

    dl_bar = Gtk.ProgressBar()
    dl_label = Gtk.Label(label="")
    dl_label.add_css_class("nvgui-muted")
    dl_label.set_xalign(0)
    dl_label.set_wrap(True)
    dl_btn = Gtk.Button(label="Download Streamline (latest)")
    dl_btn.add_css_class("nvgui-btn-primary")
    dl_body.append(dl_btn)
    dl_body.append(dl_bar)
    dl_body.append(dl_label)

    # ===== Card 3 -- per-game DLL swap / revert ===============================
    swap_card, swap_body = _card(
        "Per-game DLL swap",
        "Replace a game's nvngx_dlss.dll with a cached Streamline version. The "
        "original is backed up first; Revert restores it.",
    )
    column.append(swap_card)

    game_lbl = Gtk.Label(label="Game", xalign=0)
    game_lbl.add_css_class("nvgui-row-label")
    swap_body.append(game_lbl)
    game_dd = Gtk.DropDown()
    swap_body.append(game_dd)

    ver_lbl = Gtk.Label(label="Cached version", xalign=0)
    ver_lbl.add_css_class("nvgui-row-label")
    swap_body.append(ver_lbl)
    ver_dd = Gtk.DropDown()
    swap_body.append(ver_dd)

    swap_status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    swap_body.append(swap_status_box)

    btnrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    applyb = Gtk.Button(label="Apply swap")
    applyb.add_css_class("nvgui-btn-primary")
    applyb.set_tooltip_text("Swap this game's NVNGX_DLSS.dll with the selected cached version (backs up the original)")
    revertb = Gtk.Button(label="Revert swap")
    revertb.add_css_class("nvgui-btn-danger")
    revertb.set_tooltip_text("Restore the backed-up original NVNGX_DLSS.dll for the selected game")
    btnrow.append(applyb)
    btnrow.append(revertb)
    swap_body.append(btnrow)

    swap_label = Gtk.Label(label="")
    swap_label.add_css_class("nvgui-muted")
    swap_label.set_xalign(0)
    swap_label.set_hexpand(True)
    swap_label.set_wrap(True)
    swap_body.append(swap_label)

    # ===== Card 4 -- seed from folder (advanced) =============================
    seed_card, seed_body = _card(
        "Seed from folder (advanced)",
        "Import a local Streamline SDK bin directory as a cached version -- the "
        "power-user escape hatch for an on-disk SDK instead of the GitHub "
        "download. The version label is what the swap dropdown will show.",
    )
    column.append(seed_card)

    seed_dir = Gtk.Entry()
    seed_dir.set_placeholder_text("Path to local Streamline SDK bin directory")
    seed_dir.set_hexpand(True)
    seed_body.append(seed_dir)
    seed_ver = Gtk.Entry()
    seed_ver.set_placeholder_text("Version label, e.g. v2.12.0")
    seed_ver.set_hexpand(True)
    seed_body.append(seed_ver)
    seed_btn = Gtk.Button(label="Seed from folder")
    seed_btn.add_css_class("nvgui-btn-ghost")
    seed_body.append(seed_btn)
    seed_label = Gtk.Label(label="")
    seed_label.add_css_class("nvgui-muted")
    seed_label.set_xalign(0)
    seed_label.set_wrap(True)
    seed_body.append(seed_label)

    # ----- closures (names resolved at call time, so definition order below
    #       only needs to beat the ``connect`` + initial populate calls) -------
    def _safe(fn, default):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- a cache/library read must not crash the page
            logger.debug("dlss page op failed: %s", exc)
            return default

    def _selected_game() -> Game | None:
        games = state.get("games") or []
        if not games:
            return None
        i = game_dd.get_selected()
        if i < 0 or i >= len(games):
            return None
        return games[i]

    def _selected_version() -> str | None:
        vers = state.get("versions") or []
        if not vers:
            return None
        i = ver_dd.get_selected()
        if i < 0 or i >= len(vers):
            return None
        return vers[i]

    def _set_swap_sensitivity() -> None:
        has_game = bool(state.get("games"))
        has_ver = bool(state.get("versions"))
        applyb.set_sensitive(has_game and has_ver)
        revertb.set_sensitive(has_game)

    def render_games(games) -> None:
        state["games"] = list(games)
        labels = [f"{g.name} ({g.appid})" for g in games]
        _fill_dropdown(game_dd, labels, "(no games detected)")
        _set_swap_sensitivity()

    def render_cache(versions: list[DlssVersion]) -> None:
        _clear(cache_body)
        if not versions:
            empty = Gtk.Label(
                label="No Streamline versions cached yet -- download the latest to begin."
            )
            empty.add_css_class("nvgui-muted")
            empty.set_xalign(0)
            empty.set_wrap(True)
            cache_body.append(empty)
            return
        for v in versions:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            vname = Gtk.Label(label=v.version, xalign=0)
            vname.add_css_class("nvgui-row-label")
            vname.set_hexpand(True)
            vpath = Gtk.Label(label=v.path, xalign=0)
            vpath.add_css_class("nvgui-card-subtle")
            vpath.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            vpath.set_max_width_chars(42)
            vpath.set_hexpand(True)
            vpath.set_tooltip_text(v.path)
            row.append(vname)
            row.append(vpath)
            row.append(pill("cached", on=True))
            cache_body.append(row)

    def render_swap_versions(versions: list[DlssVersion]) -> None:
        state["versions"] = [v.version for v in versions]
        _fill_dropdown(ver_dd, state["versions"], "(no versions cached)")
        _set_swap_sensitivity()

    def refresh_swap_status() -> None:
        _clear(swap_status_box)
        game = _selected_game()
        if game is None:
            hint = Gtk.Label(label="Select a game to view its DLL swap status.")
            hint.add_css_class("nvgui-muted")
            hint.set_xalign(0)
            swap_status_box.append(hint)
            return
        try:
            status = uc.dlss_swap_status(game, None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("swap status skipped: %s", exc)
            hint = Gtk.Label(label=f"status unavailable: {exc}")
            hint.add_css_class("nvgui-muted")
            hint.set_xalign(0)
            swap_status_box.append(hint)
            return
        if not status:
            hint = Gtk.Label(label="No DLSS DLL targets found for this game.")
            hint.add_css_class("nvgui-muted")
            hint.set_xalign(0)
            swap_status_box.append(hint)
            return
        for kind, st in status.items():
            swap_status_box.append(TextRow(f"{kind}", st, accent=(st == "backed-up")))

    def refresh_all() -> None:
        """Re-read the cache once and refresh the list, the swap dropdown, and
        the per-game status together (the download's on_done lands here)."""
        versions = _safe(uc.list_dlss_versions, [])
        render_cache(versions)
        render_swap_versions(versions)
        refresh_swap_status()

    def _on_apply(_b) -> None:
        game = _selected_game()
        if game is None:
            swap_label.set_text("Select a game first.")
            return
        ver = _selected_version()
        if not ver:
            swap_label.set_text("Download a Streamline version first.")
            return
        try:
            res = uc.apply_dlss_swap(game, ver, None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dlss apply swap failed: %s", exc)
            swap_label.set_text(f"swap failed: {exc}")
            return
        swap_label.set_text(res.message if res.ok else f"swap: {res.message}")
        # Transient confirmation on success; failures stay on the persistent
        # inline label (a toast would flash + hide the error before it's read).
        if res.ok and on_status is not None:
            on_status(res.message)
        refresh_swap_status()

    def _on_revert(_b) -> None:
        game = _selected_game()
        if game is None:
            swap_label.set_text("Select a game first.")
            return
        # Snapshot the game into the closure: between this ``clicked`` firing and
        # the user picking "Revert" in the async AlertDialog, the DropDown's
        # selected game can change. Pin the click-time game + the clicked button
        # (for modal parenting) so the confirm acts on exactly what was shown.
        def _do_revert(pinned: "Game", btn: Gtk.Button) -> None:
            try:
                res = uc.revert_dlss_swap(pinned, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("dlss revert swap failed: %s", exc)
                swap_label.set_text(f"revert failed: {exc}")
                return
            swap_label.set_text(res.message if res.ok else f"revert: {res.message}")
            if res.ok and on_status is not None:
                on_status(res.message)
            refresh_swap_status()

        confirm_destructive(
            toplevel_of(_b),
            "Revert the DLL swap?",
            detail="Restore the backed-up original NVNGX_DLSS.dll for this game.",
            confirm_label="Revert",
            on_confirm=lambda pinned=game, btn=_b: _do_revert(pinned, btn),
        )

    def _on_seed(_b) -> None:
        src = seed_dir.get_text().strip()
        lbl = seed_ver.get_text().strip()
        if not src:
            seed_label.set_text("Enter a source directory first.")
            return
        if not lbl:
            seed_label.set_text("Enter a version label first.")
            return
        seed_btn.set_sensitive(False)
        seed_label.set_text(f"Seeding {lbl} from {src}...")

        def work() -> None:
            try:
                ver = uc.seed_dlss_from(src, lbl)
                GLib.idle_add(
                    lambda v=ver: _after_seed(True, f"Seeded {v.version} from {src}")
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("dlss seed failed: %s", exc)
                GLib.idle_add(lambda e=exc: _after_seed(False, f"seed failed: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _after_seed(ok: bool, msg: str) -> bool:
        seed_btn.set_sensitive(True)
        seed_label.set_text(msg)
        if ok:
            refresh_all()
        return False  # one-shot idle source

    # ----- wiring + initial populate ------------------------------------------
    dl_btn.connect(
        "clicked",
        lambda _b: StreamlineDownloader.start(
            uc, dl_bar, dl_label, on_done=refresh_all
        ),
    )
    applyb.connect("clicked", _on_apply)
    revertb.connect("clicked", _on_revert)
    seed_btn.connect("clicked", _on_seed)
    game_dd.connect("notify::selected", lambda _dd, _ps: refresh_swap_status())

    render_games(_safe(uc.scan_games, []))
    refresh_all()
    return _scrolled(column)
