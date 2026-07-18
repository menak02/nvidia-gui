"""Steam game-library adapter — parses libraryfolders.vdf + appmanifest_*.acf."""

from __future__ import annotations

import logging
import pathlib
from typing import Iterable

from ..application.ports import GameLibraryPort
from ..domain.models import Game, Launcher
from .vdf_lite import loads

logger = logging.getLogger(__name__)

# --- Steam tool / runtime / redistributable filter --------------------------
# Steam installs non-game "tool" manifests BESIDE real games under steamapps/
# (Proton variants, the Steam Linux Runtimes, Steamworks Common Redistributables).
# The ACF carries NO ``app_type`` field -- Valve encodes tool-vs-game only in the
# private Steam registry, not the manifest -- so the parser must discriminate
# from stable on-disk tells: the ``installdir`` (tools install under
# ``SteamLinuxRuntime_*`` / ``Proton*`` / the redist dir ``Steamworks Shared``;
# real games never colocate there) plus the display name, AND a curated Valve-
# tool appid blocklist as belt-and-suspenders for any tool that renames. The
# installdir/name patterns catch tools that join Steam LATER (a future Proton
# 11.0 / SLR 5.0 ships with the same naming convention), so the filter ages
# with Steam rather than needing every new tool appid hand-added.
_STEAM_TOOL_APPIDS: frozenset[str] = frozenset({
    "228980",   # Steamworks Common Redistributables (depots that games share)
    "1070560",  # Steam Linux Runtime 1.0 (scout)
    "1391110",  # Steam Linux Runtime 2.0 (soldier)
    "1628350",  # Steam Linux Runtime 3.0 (sniper)
    "4183110",  # Steam Linux Runtime 4.0
    "1493710",  # Proton Experimental
    "2180100",  # Proton Hotfix
    "3658110",  # Proton 10.0
})


def _looks_like_steam_tool(name: str, installdir: str, appid: str) -> bool:
    """True if this manifest is a Steam tool/runtime/redistributable, not a game.

    Order: curated appid blocklist first (decisive for known tools), then the
    installdir prefix / exact-match tells (future-proof), then the display-name
    fallback. A real game has never shipped under one of these installdirs.
    """
    if appid in _STEAM_TOOL_APPIDS:
        return True
    inst = installdir.lower().strip()
    if inst.startswith("steamlinuxruntime"):
        return True
    # "Proton - Experimental", "Proton Hotfix", "Proton 10.0" all install under
    # an installdir beginning "Proton"; games do not.
    if inst == "proton" or inst.startswith("proton ") or inst.startswith("proton-"):
        return True
    if inst == "steamworks shared":
        return True
    nm = name.lower()
    if "steamworks common redistributables" in nm:
        return True
    return False


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
        # skip empty manifests
        if not name or not installdir:
            return None
        # skip Steam tools/runtimes/redistributables that install beside games
        # under steamapps/ (Proton, SLR, Steamworks Redistributables) -- they
        # would otherwise show up as fake "games" the user can't launch. See
        # :func:`_looks_like_steam_tool` for the discriminator rationale.
        if _looks_like_steam_tool(name, installdir, appid):
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
