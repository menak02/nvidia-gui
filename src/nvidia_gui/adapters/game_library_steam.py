"""Steam game-library adapter — parses libraryfolders.vdf + appmanifest_*.acf."""

from __future__ import annotations

import logging
import pathlib
from typing import Iterable

from ..application.ports import GameLibraryPort
from ..domain.models import Game, Launcher
from .vdf_lite import loads

logger = logging.getLogger(__name__)


class SteamLibrary(GameLibraryPort):
    """Scan one or more Steam library roots for installed appmanifests.

    Games whose manifest lacks an installdir or name, or that are not
    ``StatefullyInstalled`` (e.g. tool manifests without content), are skipped.
    """

    def __init__(self, steam_root: str | pathlib.Path | None = None) -> None:
        self._root = pathlib.Path(
            steam_root or (pathlib.Path.home() / ".local/share/Steam")
        ).expanduser()

    def scan(self) -> list[Game]:
        roots = self._library_roots()
        games: list[Game] = []
        seen: set[str] = set()
        for root in roots:
            steamapps = root
            for acf in sorted(steamapps.glob("appmanifest_*.acf")):
                appid = acf.stem.removeprefix("appmanifest_")
                if appid in seen:
                    continue
                game = self._parse_acf(acf, appid)
                if game:
                    seen.add(appid)
                    games.append(game)
        # sort by name for stable UI
        games.sort(key=lambda g: g.name.lower())
        return games

    def _library_roots(self) -> list[pathlib.Path]:
        lf = self._root / "steamapps" / "libraryfolders.vdf"
        roots = [self._root / "steamapps"]
        if not lf.is_file():
            return roots
        try:
            data = loads(lf.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("bad libraryfolders.vdf: %s", exc)
            return roots
        libs = data.get("libraryfolders", {})
        for _, v in libs.items():
            if not isinstance(v, dict):
                continue
            p = v.get("path")  # libraryfolders entry: the library root dir
            if p and pathlib.Path(p).is_dir():
                roots.append(pathlib.Path(p) / "steamapps")
        # de-dup, keep order
        out: list[pathlib.Path] = []
        for r in roots:
            if r not in out:
                out.append(r)
        return out

    def _parse_acf(self, acf: pathlib.Path, appid: str) -> Game | None:
        try:
            data = loads(acf.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("bad %s: %s", acf.name, exc)
            return None
        st = data.get("AppState", {})
        if not isinstance(st, dict):
            return None
        name = st.get("name") or ""
        installdir = st.get("installdir") or ""
        # skip tools/empty manifests
        if not name or not installdir:
            return None
        return Game(
            appid=appid,
            name=str(name).strip(),
            installdir=str(installdir).strip(),
            launcher=Launcher.STEAM,
        )

    # public helper for adapters that need the absolute install path
    def install_path(self, installdir: str) -> pathlib.Path | None:
        for root in self._library_roots():
            p = root / "common" / installdir
            if p.is_dir():
                return p
        return None
