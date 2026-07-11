"""NVIDIAApp — the GTK4 Application lifecycle.

The presentation layer owns no adapters: ``build_app`` (composition root) hands
us the use cases and the already-wired IPC server; we present the window and
start/stop the server. No ``adapters.*`` import lives here.
"""

from __future__ import annotations

import logging
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk  # noqa: E402

from ..composition_root import build_app  # noqa: E402
from .theme import load_theme  # noqa: E402
from .window import MainWindow  # noqa: E402

logger = logging.getLogger(__name__)


class NVIDIAApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id="org.mena.nvidia-gui",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.uc = None
        self._ipc = None
        self.connect("activate", self._on_activate)

    def _on_activate(self, _app) -> None:
        logger.info("activate")
        load_theme()
        if self.uc is None:
            self.uc, self._ipc = build_app()
        win = MainWindow(self, self.uc)
        win.present()
        if self._ipc is not None:
            self._ipc.start()

    def do_shutdown(self) -> None:
        if self._ipc is not None:
            self._ipc.stop()
        Gtk.Application.do_shutdown(self)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s"
    )
    app = NVIDIAApp()
    return app.run(argv or sys.argv)
