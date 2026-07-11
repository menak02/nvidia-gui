"""The ONE Streamline/DLSS download worker — shared by every consumer.

Extracted from the old inline ``views._start_download`` so the new DLSS page
and the existing views all share a single download surface instead of each
re-implementing the worker-thread + ``GLib.idle_add`` hop. The ~221 MB fetch
runs off the GTK main loop; only low-cost fraction/label updates hop back.
Imports no adapter — drives :meth:`UseCases.download_streamline_release`, as
the hexagonal seam requires. ``on_done`` is a no-arg callback invoked on the
main loop when the download resolves (used by views that rebuild after fetch).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

if TYPE_CHECKING:
    from ..application.use_cases import UseCases

logger = logging.getLogger(__name__)


class StreamlineDownloader:
    """Fetch the latest Streamline release behind a worker thread, mirroring
    progress through a ``Gtk.ProgressBar`` + ``Gtk.Label`` on the main loop."""

    def __init__(self, uc: "UseCases") -> None:
        self.uc = uc

    @classmethod
    def start(
        cls,
        uc: "UseCases",
        bar: Gtk.ProgressBar,
        label: Gtk.Label,
        on_done=None,
    ) -> "StreamlineDownloader":
        """Construct and launch in one call (the common path). Returns the
        instance in case the caller wants to track it."""
        self = cls(uc)
        self.launch(bar, label, on_done)
        return self

    def launch(
        self,
        bar: Gtk.ProgressBar,
        label: Gtk.Label,
        on_done=None,
    ) -> None:
        bar.set_fraction(0.0)
        bar.set_show_text(True)
        label.set_text("Resolving latest Streamline release…")
        state = {"last": -1.0}

        def _progress(done: int, total: int) -> None:
            if total:
                frac = done / total
                # throttle to ~1% steps so a 221 MB download isn't thousands of
                # idle posts stealing the main loop.
                if frac - state["last"] >= 0.01:
                    state["last"] = frac
                    GLib.idle_add(lambda f=frac: bar.set_fraction(f))
            else:
                GLib.idle_add(lambda: bar.pulse())

        def _done(ok: bool, msg: str) -> bool:
            bar.set_fraction(1.0 if ok else 0.0)
            # ASCII-only status — this app forbids emoji-band codepoints
            label.set_text(f"{'OK' if ok else 'Failed'} - {msg}")
            if on_done is not None:
                on_done()
            return False

        def work() -> None:
            try:
                ver = self.uc.download_streamline_release(_progress)
                GLib.idle_add(lambda: _done(True, f"Downloaded {ver.version}"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("dlss download failed: %s", exc)
                GLib.idle_add(lambda: _done(False, str(exc)))

        threading.Thread(target=work, daemon=True).start()
