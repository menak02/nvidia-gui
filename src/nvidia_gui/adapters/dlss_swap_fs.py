"""DLSS DLL swap — copies a cached bundle into a game's dirs, with backups."""

from __future__ import annotations

import logging
import pathlib
import shutil
from typing import Callable

from ..application.ports import DlssSwapPort
from ..domain import services
from ..domain.models import DlssBundle, Game

logger = logging.getLogger(__name__)

_BAK_SUFFIX = ".nvagogui.orig"


class FsDlssSwap(DlssSwapPort):
    """Applies swaps by copying; reverts by restoring the .orig backup.

    We never delete files we didn't create: an original DLL found at a target
    is moved aside to ``<name>.nvagogui.orig`` before our copy lands, and a
    subsequent revert pulls the backup back. Our own files (the ones we wrote)
    are removed on revert so the game falls back to its backup exactly.

    ``resolve_install`` maps a Game to its absolute install dir. The
    composition root injects the library's resolver so this adapter never
    imports a peer adapter; tests inject a fake (or ``None`` ⇒ no targets).
    """

    def __init__(
        self,
        resolve_install: Callable[[Game], str | None] | None = None,
    ) -> None:
        self._resolve_install = resolve_install

    def apply(
        self,
        game: Game,
        bundle: DlssBundle,
        prefix_system32_abs: str | None,
    ) -> tuple[list[str], list[str]]:
        installdir_abs = self._install_path(game)
        if installdir_abs is None:
            return [], []
        kinds = [k for k, _ in bundle.available_dlls()]
        targets = services.dlss_target_paths(installdir_abs, prefix_system32_abs, kinds)
        written: list[str] = []
        backups: list[str] = []
        for kind, target in targets:
            src_name = services.dll_filename(kind)
            src = pathlib.Path(bundle.path) / src_name
            if not src.is_file():
                continue
            tpath = pathlib.Path(target)
            existing = tpath if tpath.is_file() else None
            # back up existing *non-our* original only once
            if existing and not pathlib.Path(str(tpath) + _BAK_SUFFIX).exists():
                bak = pathlib.Path(str(tpath) + _BAK_SUFFIX)
                shutil.copy2(tpath, bak)
                backups.append(str(bak))
            tpath.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tpath)
            written.append(str(tpath))
        return written, backups

    def revert(self, game: Game, prefix_system32_abs: str | None) -> list[str]:
        installdir_abs = self._install_path(game)
        if installdir_abs is None:
            return []
        restored: list[str] = []
        # gather all target paths for every known DLL kind, even ones not in a
        # specific bundle, so a stale swap from a removed version still reverts.
        kinds = services.dll_kinds()
        targets = services.dlss_target_paths(installdir_abs, prefix_system32_abs, kinds)
        seen: set[str] = set()
        for _kind, target in targets:
            if target in seen:
                continue
            seen.add(target)
            tpath = pathlib.Path(target)
            bak = pathlib.Path(str(tpath) + _BAK_SUFFIX)
            if bak.is_file():
                shutil.copy2(bak, tpath)
                bak.unlink()
                restored.append(str(tpath))
        return restored

    def status(self, game: Game, prefix_system32_abs: str | None) -> dict[str, str]:
        installdir_abs = self._install_path(game)
        if installdir_abs is None:
            return {k: "missing" for k in services.dll_kinds()}
        kinds = services.dll_kinds()
        targets = services.dlss_target_paths(installdir_abs, prefix_system32_abs, kinds)
        # A kind resolves to several candidate locations (game root + subdirs +
        # prefix system32); Proton searches them in order, so a DLL at *any* one
        # means the kind is swappable. Take the strongest signal across them — a
        # later missing candidate must not clobber an earlier hit. (The original
        # last-write-wins loop reported "missing" whenever the final candidate,
        # Binaries/Win32, lacked the file, even if the game root held it.)
        _rank = {"missing": 0, "present": 1, "backed-up": 2}
        out: dict[str, str] = {}
        for kind, target in targets:
            tpath = pathlib.Path(target)
            bak = pathlib.Path(str(tpath) + _BAK_SUFFIX)
            cur = "backed-up" if bak.is_file() else ("present" if tpath.is_file() else "missing")
            if _rank.get(out.get(kind), 0) < _rank[cur]:
                out[kind] = cur
        for k in kinds:
            out.setdefault(k, "missing")
        return out

    # ------------------------------------------------------------------
    def _install_path(self, game: Game) -> str | None:
        """Resolve the game's absolute install dir via the injected resolver."""
        if self._resolve_install is None:
            return None
        return self._resolve_install(game)
