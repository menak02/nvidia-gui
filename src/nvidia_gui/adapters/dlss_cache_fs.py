"""Managed DLSS cache — one folder per version, populated from Streamline.

The cache lives under ``$XDG_DATA_HOME/nvidia-gui/dlss_cache/<version>/``. Each
version directory holds the Streamline DLLs imported with it. The normal path
is :meth:`FsDlssCache.download_latest_release`, which fetches the newest
Streamline GitHub release zip and extracts only the known DLLs.
:meth:`seed_from` remains as an escape hatch for a power user with a local
SDK checkout.
"""

from __future__ import annotations

import json
import logging
import pathlib
import shutil
import tempfile
import urllib.request
import zipfile

from ..application.ports import DlssCachePort
from ..domain.models import DlssBundle, DlssVersion

logger = logging.getLogger(__name__)

# kind -> filename the bundle stores
_DLL_FILES = {
    "dlss": "nvngx_dlss.dll",
    "dlssd": "nvngx_dlssd.dll",
    "dlssg": "nvngx_dlssg.dll",
    "low_latency_vk": "NvLowLatencyVk.dll",
    "deepdvc": "nvngx_deepdvc.dll",
}


class FsDlssCache(DlssCachePort):
    def __init__(self, cache_dir: pathlib.Path) -> None:
        self._root = pathlib.Path(cache_dir)
        self._root.mkdir(parents=True, exist_ok=True)

    def cache_dir(self) -> str:
        return str(self._root)

    def list_versions(self) -> list[DlssVersion]:
        out: list[DlssVersion] = []
        for d in sorted(self._root.iterdir()):
            if d.is_dir() and (d / "nvngx_dlss.dll").is_file():
                out.append(DlssVersion(version=d.name, path=str(d)))
        return out

    def get_bundle(self, version: str) -> DlssBundle | None:
        d = self._root / version
        if not d.is_dir():
            return None
        parts: dict[str, str] = {}
        for kind, fname in _DLL_FILES.items():
            if (d / fname).is_file():
                parts[kind] = fname
        if "dlss" not in parts:
            return None  # not a valid bundle
        return DlssBundle(version=version, **parts)

    def seed_from(self, source_dir: str, version_label: str) -> DlssVersion:
        src = pathlib.Path(source_dir).expanduser()
        if not src.is_dir():
            raise FileNotFoundError(f"DLSS seed source not found: {src}")
        dest = self._root / version_label
        dest.mkdir(parents=True, exist_ok=True)
        imported: list[str] = []
        for kind, fname in _DLL_FILES.items():
            sfile = src / fname
            if sfile.is_file():
                shutil.copy2(sfile, dest / fname)
                imported.append(fname)
        if "nvngx_dlss.dll" not in imported:
            # not a real Streamline bin dir — clean up partial import
            if not any((dest).glob("nvngx_dlss.dll")):
                logger.warning("seed source had no nvngx_dlss.dll: %s", src)
        logger.info("seeded %s from %s -> %d DLL(s)", version_label, src, len(imported))
        return DlssVersion(version=version_label, path=str(dest))

    # ---- GitHub release fetch ---------------------------------------------
    _STREAMLINE_REPO = "NVIDIA-RTX/Streamline"
    _UA = "nvidia-gui (Streamline cache fetcher)"

    def download_latest_release(
        self, progress=None
    ) -> DlssVersion:
        """Fetch the newest Streamline release from GitHub and extract the
        known DLLs into the cache under the release tag (e.g. ``v2.12.0``).

        Network access lives in the adapter (its job) but this method is
        synchronous and blocking — callers MUST run it off the GTK main loop.
        ``progress(downloaded, total)`` is invoked on this (worker) thread as
        the zip streams down, so a view can hop it onto the UI loop.
        """
        api = f"https://api.github.com/repos/{self._STREAMLINE_REPO}/releases/latest"
        req = urllib.request.Request(
            api, headers={"Accept": "application/vnd.github+json",
                          "User-Agent": self._UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            meta = json.loads(r.read().decode("utf-8"))
        tag = meta.get("tag_name")
        if not tag:
            # A release JSON without tag_name is malformed (a mirror quirk or
            # a rate-limit body that happened to decode) -- raise so the worker
            # surfaces "Failed - ..." instead of silently filing the download
            # under a bogus version id "streamline" presented as a success.
            raise RuntimeError("latest Streamline release has no tag_name")
        asset = None
        for a in meta.get("assets", []):
            name = (a.get("name") or "").lower()
            if name.endswith(".zip") and "streamline" in name:
                asset = a
                break
        if asset is None:
            raise RuntimeError("latest Streamline release has no SDK zip asset")
        url = asset["browser_download_url"]
        total = int(asset.get("size") or 0)

        dest = self._root / tag
        dest.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="streamline-", suffix=".zip",
                                         delete=False) as tf:
            tmp = pathlib.Path(tf.name)
        try:
            self._download_zip(url, tmp, total, progress)
            self._extract_dlls(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        logger.info("downloaded Streamline %s -> %d DLL(s) in %s",
                    tag, len(_DLL_FILES), dest)
        return DlssVersion(version=tag, path=str(dest))

    def _download_zip(self, url, dest, total, progress) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": self._UA})
        with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
            got = 0
            while True:
                chunk = r.read(1 << 16)  # 64 KiB
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress is not None:
                    progress(got, total)

    def _extract_dlls(self, zip_path, dest) -> None:
        """Pull only the known DLLs out of the zip, robust to its internal layout."""
        wanted = set(_DLL_FILES.values())
        hits: dict[str, list[str]] = {f: [] for f in wanted}
        with zipfile.ZipFile(zip_path) as z:
            for name in z.namelist():
                if name.endswith("/"):
                    continue
                base = name.rsplit("/", 1)[-1]
                if base in wanted and base.lower().endswith(".dll"):
                    hits[base].append(name)
            for base, names in hits.items():
                if not names:
                    continue
                chosen = self._best_candidate(names)
                with z.open(chosen) as src, open(dest / base, "wb") as outt:
                    shutil.copyfileobj(src, outt)
        if not ((dest / _DLL_FILES["dlss"]).is_file()):
            raise RuntimeError("Streamline zip had no nvngx_dlss.dll — unexpected layout")

    @staticmethod
    def _best_candidate(names: list[str]) -> str:
        """Prefer the copy that lives under the SDK's release bin/x64 dir."""
        for pref in ("/bin/x64/", "/x64/", "/bin/"):
            for n in names:
                if pref in n:
                    return n
        return names[0]
