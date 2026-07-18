"""Per-game NVIDIA-feature detection (DLSS-SR / Frame Gen / Reflex / RT).

Confidence-tiered merge for the four features {dlss_sr, dlss_fg, reflex, rt}:

    user override  >  online community-DB (cached, offline-graceful)  >
    bundled assets/nvidia_features.toml  >  install-dir DLL probe  >
    prefix DLL probe  >  UNKNOWN

Offline-graceful: :meth:`SteamFeatureDetector.probe` NEVER raises -- a network
failure or missing DB just degrades to lower tiers / UNKNOWN with a ``notes``
explanation. The cardinal rule: never silently report ``True`` (that was the
old unconditional-toggle bug -- 'not all games come with them').

Reuse, don't rebuild: the ``resolve_install`` closure (built in
:mod:`composition_root` for DLSS-swap) for install-dir probes; the curated
``_NVAPI_DLLS`` name set from :mod:`adapters.diagnostics` for prefix probes
(importing the constant for the DLL name list only -- not the adapter as a
dependency); the injected ``settings`` port (``SettingsPort``) for overrides;
``bundled_db`` path (``assets/nvidia_features.toml``) for the bundled floor.
No peer-adapter module is imported; only the curated constant and the
stdlib (``tomllib``, ``urllib``, ``json``, ``pathlib``).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import time
import tomllib
import urllib.request

from ..application.ports import FeatureDetectionPort
from ..domain.models import (
    FeatureFlag,
    FeatureSource,
    Game,
    GameCapability,
)
from .diagnostics import _NVAPI_DLLS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pluggable online community-DB endpoint.
# ---------------------------------------------------------------------------
# The online tier fetches a community-maintained DLSS-games index and caches
# it at ~/.cache/nvidia-gui/feature_cache.json (TTL ~24h). No single public
# community endpoint is reliably maintained in 2026 (the classic dlss-bs-swapper
# / SteamDB-derived JSON lists come and go), so this tier is OPT-IN and
# OFFLINE-SAFE by default: unless an operator sets
# ``NVIDIA_GUI_FEATURE_DB_URL`` in the environment, the tier degrades to skip
# (notes carry "offline") and detection falls through to the bundled DB + DLL
# probes -- the deterministic offline floor. Rationale: a strict first-hit-wins
# pipeline where the online tier runs unconditionally would resolve every
# listed game at the "community-db" source on any internet-connected machine,
# which both (a) breaks the architect's deterministic strict-verify assert
# (Cyberpunk 1091500 must read source == "curated") and (b) reintroduces the
# false-positive risk the whole batch exists to kill, since community lists are
# best-effort. The recommended template is a Steam appdetails-driven shape:
#   https://store.steampowered.com/api/appdetails?appids={appid}&l=en&cc=us
# parsed for the appid in question (a per-game probe, not a bulk mirror); but
# because Steam appdetails does not expose an explicit DLSS flag, an operator
# who actually wants this tier should point the URL at a real DLSS-games JSON
# whose entries carry dlss_sr/dlss_fg/reflex/rt booleans.

_FEATURE_DB_URL = os.environ.get("NVIDIA_GUI_FEATURE_DB_URL", "")
_CACHE_PATH = pathlib.Path.home() / ".cache" / "nvidia-gui" / "feature_cache.json"
_CACHE_TTL_S = 24 * 60 * 60  # ~24h
_HTTP_TIMEOUT = 6.0

# The four feature kinds, in the order GameCapability exposes them.
_KINDS = ("dlss_sr", "dlss_fg", "reflex", "rt")


class SteamFeatureDetector(FeatureDetectionPort):
    """Steam per-game NVIDIA-feature detector with a confidence-tiered merge.

    ``resolve_install`` resolves a :class:`Game` to its absolute Steam install
    dir (the closure :mod:`composition_root` builds for the DLSS-swap adapter),
    so the install-dir DLL probe reuses it -- no peer-adapter import.
    ``settings`` is the :class:`SettingsPort` so overrides live in app-config
    (may be ``None`` in tests -- then there is simply no override tier).
    ``bundled_db`` points at ``assets/nvidia_features.toml``.
    """

    def __init__(
        self,
        *,
        resolve_install,
        steam_root: str,
        settings,
        bundled_db: pathlib.Path,
    ) -> None:
        self._resolve_install = resolve_install
        self._steam_root = steam_root
        self._settings = settings
        self._bundled_db = pathlib.Path(bundled_db)

    # ------------------------------------------------------------------
    # probe -- the tiered pipeline. NEVER raises.
    # ------------------------------------------------------------------
    def probe(self, game: Game) -> GameCapability:  # noqa: D401
        try:
            return self._probe_safe(game)
        except Exception as exc:  # noqa: BLE001 -- the hard contract: never raise
            logger.warning("feature detection failed for %s: %s", game.appid, exc)
            return GameCapability(
                appid=game.appid,
                notes=f"detection error: {exc!r}",
            )

    def _probe_safe(self, game: Game) -> GameCapability:
        notes: list[str] = []

        # --- Tier 1: user override (notes on settings-port read failure) ---
        ov = self._override_tier(game, notes)

        # --- Tier 2: online + Tier 3: bundled ---
        # Each tier yields a flags dict ({} = nothing usable at this tier) plus
        # an "offline" hint when the online tier was skipped/degraded; the
        # merge helpers carry the correct provenance (ONLINE vs BUNDLED) via
        # which dict the bool came from.
        online_flags, offline = self._online_tier(game, notes)
        bundled_flags = self._bundled_tier(game, notes) or {}
        online_flags = online_flags or {}
        if offline and not any("offline" in n or "unreachable" in n for n in notes):
            notes.append("offline")

        # --- Tier 4: install-dir + Tier 5: prefix DLL probes ---
        sr_install, dlssg_install = self._install_dir_probe(game, notes)
        sr_prefix, dlssg_prefix = self._prefix_probe(game, notes)

        # dlss_sr / reflex: strict first-hit-wins (override > online > bundled
        # > install-dir DLL > prefix DLL > UNKNOWN).
        dlss_sr = self._singleton(ov.get("dlss_sr"),
                                  online_flags.get("dlss_sr"),
                                  bundled_flags.get("dlss_sr"),
                                  sr_install, sr_prefix)
        reflex = self._singleton(ov.get("reflex"),
                                 online_flags.get("reflex"),
                                 bundled_flags.get("reflex"),
                                 dlssg_install, dlssg_prefix)

        # dlss_fg / rt: OR-style merges per the spec's special rules.
        dlss_fg = self._merge_fg(
            ov.get("dlss_fg"),
            online_flags.get("dlss_fg"), bundled_flags.get("dlss_fg"),
            dlss_sr, dlssg_install, dlssg_prefix,
        )
        rt = self._merge_rt(
            ov.get("rt"),
            online_flags.get("rt"), bundled_flags.get("rt"),
            sr_install, sr_prefix,
        )

        if not notes:
            notes.append(self._default_note(dlss_sr, reflex, rt))

        return GameCapability(
            appid=game.appid,
            dlss_sr=dlss_sr,
            dlss_fg=dlss_fg,
            reflex=reflex,
            rt=rt,
            notes="; ".join(notes),
        )

    # ==================================================================
    # Tier 1 -- user override
    # ==================================================================
    def _override_tier(self, game: Game, notes: list[str]) -> dict[str, bool]:
        """Return only the boolean overrides (auto/None/absent are skipped).

        A settings-port READ failure must NOT silently look like "no override"
        -- that would let a corrupt store quietly drop a user's explicit
        override (the silent lie the hunter flagged). On the first kind that
        raises we note the failure (so the user sees *why* their override did
        not take) and stop reading further kinds for this game.
        """
        out: dict[str, bool] = {}
        if self._settings is None:
            return out
        for kind in _KINDS:
            try:
                raw = self._settings.get(f"feature.{game.appid}.{kind}")
            except Exception as exc:  # noqa: BLE001 -- never raise
                if not any("override read failed" in n for n in notes):
                    notes.append(f"override read failed ({exc.__class__.__name__})")
                break
            if isinstance(raw, bool):
                out[kind] = raw
        return out

    # ==================================================================
    # Tier 2 -- online community-DB
    # ==================================================================
    def _online_tier(
        self, game: Game, notes: list[str]
    ) -> tuple[dict[str, bool | None] | None, bool]:
        """Fetch+cache a community DLSS-games index; return ``(flags, offline)``.

        ``offline`` is True when the tier was skipped because no URL is set OR a
        network/parse/cache failure degraded it -- the caller surfaces this in
        ``notes``. Never raises.
        """
        url = _FEATURE_DB_URL
        if not url:
            # OPT-IN offline-safe default (see module docstring): skip the tier.
            return None, True
        try:
            data = self._fetch_or_cache(url, notes)
            return self._extract_online_entry(data, game.appid), False
        except Exception as exc:  # noqa: BLE001
            logger.debug("online feature-db tier skipped for %s: %s",
                         game.appid, exc)
            notes.append(f"community DB unreachable ({exc.__class__.__name__})")
            return None, True

    def _extract_online_entry(
        self, data: dict, appid: str
    ) -> dict[str, bool | None] | None:
        """Pull a single appid's flags out of a cached JSON document."""
        if not isinstance(data, dict):
            return None
        # Tolerate a handful of plausible community-DB shapes without raising.
        games = (
            data.get("games")
            or data.get("apps")
            or data.get("entries")
            or (data if self._looks_like_a_games_map(data) else None)
        )
        if isinstance(games, dict):
            entry = games.get(appid)
        elif isinstance(games, list):
            entry = next(
                (g for g in games
                 if isinstance(g, dict) and str(g.get("appid")) == appid),
                None,
            )
        else:
            entry = None
        if not isinstance(entry, dict):
            return None
        out: dict[str, bool | None] = {}
        for key in ("dlss_sr", "dlss_fg", "reflex", "rt"):
            v = entry.get(key)
            out[key] = v if isinstance(v, bool) else None
        return out or None

    @staticmethod
    def _looks_like_a_games_map(data: dict) -> bool:
        """True if *data* is itself ``{appid: {...}}`` (keys are appid strings)."""
        for k, v in data.items():
            if not (isinstance(k, str) and k.isdigit() and isinstance(v, dict)):
                return False
        return bool(data)

    def _singleton(
        self,
        override: bool | None,
        online: bool | None,
        bundled: bool | None,
        install_dll: bool,
        prefix_dll: bool,
    ) -> FeatureFlag:
        """Strict first-hit-wins for dlss_sr / reflex.

        Override > online > bundled > install-dir DLL > prefix DLL > UNKNOWN.
        A DB-sourced ``False`` is a real (decisive) value -- it WINS over the
        DLL-fallback tiers (the DLL absence doesn't prove non-support for a
        game the DB already listed below UNKNOWN). This keeps a curated
        ``dlss_sr=false`` row authoritative rather than letting a stray
        install-dir probe flip it.
        """
        if override is not None:
            return self._flag(override, FeatureSource.OVERRIDE)
        if online is not None:
            return self._flag(online, FeatureSource.ONLINE)
        if bundled is not None:
            return self._flag(bundled, FeatureSource.BUNDLED)
        if install_dll:
            return self._flag(True, FeatureSource.INSTALLDIR)
        if prefix_dll:
            return self._flag(True, FeatureSource.PREFIX)
        return self._flag_unknown()

    def _merge_fg(
        self,
        override: bool | None,
        online_fg: bool | None,
        bundled_fg: bool | None,
        dlss_sr: FeatureFlag,
        dlssg_install: bool,
        dlssg_prefix: bool,
    ) -> FeatureFlag:
        """dlss_fg: supported only if DLSS>=3.5 evidence exists.

        A present ``nvngx_dlssg.dll`` in the install dir / prefix is the
        strongest DLSS>=3.5 signal (Frame Gen DLL ships with DLSS 3.5+). A
        positive online/bundled flag also qualifies -- but ONLY when dlss_sr
        resolved above UNKNOWN (the spec's gate), so a curated-data False on
        dlss_sr cannot carry a True dlss_fg. Never silently True on dlss_sr
        alone.
        """
        if override is not None:
            return self._flag(override, FeatureSource.OVERRIDE)
        if dlssg_install:
            return self._flag(True, FeatureSource.INSTALLDIR)
        if dlssg_prefix:
            return self._flag(True, FeatureSource.PREFIX)
        sr_known = dlss_sr.source != FeatureSource.UNKNOWN and dlss_sr.supported
        if sr_known and online_fg is True:
            return self._flag(True, FeatureSource.ONLINE)
        if sr_known and bundled_fg is True:
            return self._flag(True, FeatureSource.BUNDLED)
        # A DB False is explicitly "no frame gen" -- report it as a known False
        # (above UNKNOWN) so the UI can desaturate rather than show unknown.
        if online_fg is False:
            return self._flag(False, FeatureSource.ONLINE)
        if bundled_fg is False:
            return self._flag(False, FeatureSource.BUNDLED)
        return self._flag_unknown()

    def _merge_rt(
        self,
        override: bool | None,
        online_rt: bool | None,
        bundled_rt: bool | None,
        sr_install: bool,
        sr_prefix: bool,
    ) -> FeatureFlag:
        """rt: supported if DLSS DLL present (install/prefix) OR DB flags it.

        OR-style per the spec: a present nvngx_dlss.dll (the install-dir / prefix
        probe) OR an online/bundled rt=True all qualify. A DB False is a known
        negative (reported above UNKNOWN so the UI desaturates honestly), but a
        DLL present still wins as positive evidence. Override always wins.
        """
        if override is not None:
            return self._flag(override, FeatureSource.OVERRIDE)
        if online_rt is True:
            return self._flag(True, FeatureSource.ONLINE)
        if bundled_rt is True:
            return self._flag(True, FeatureSource.BUNDLED)
        if sr_install:
            return self._flag(True, FeatureSource.INSTALLDIR)
        if sr_prefix:
            return self._flag(True, FeatureSource.PREFIX)
        if online_rt is False:
            return self._flag(False, FeatureSource.ONLINE)
        if bundled_rt is False:
            return self._flag(False, FeatureSource.BUNDLED)
        return self._flag_unknown()

    # ==================================================================
    # Tier 3 -- bundled curated DB
    # ==================================================================
    def _bundled_tier(self, game: Game, notes: list[str]) -> dict[str, bool] | None:
        try:
            with open(self._bundled_db, "rb") as fh:
                doc = tomllib.load(fh)
        except (FileNotFoundError, tomllib.TOMLDecodeError, OSError) as exc:
            # A corrupt/missing bundled DB must NOT look identical to "found
            # nothing for this appid" -- note the read failure so the offline
            # floor is honest about whether the DB is even readable.
            if not any("bundled DB unreadable" in n for n in notes):
                notes.append(f"bundled DB unreadable ({exc.__class__.__name__})")
            logger.debug("bundled feature-db unreadable: %s", exc)
            return None
        rows = doc.get("games") if isinstance(doc, dict) else None
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("appid")) == game.appid:
                out = {}
                for k in ("dlss_sr", "dlss_fg", "reflex", "rt"):
                    v = row.get(k)
                    if isinstance(v, bool):
                        out[k] = v
                return out or None
        return None

    # ==================================================================
    # Tier 4 -- install-dir DLL probe
    # ==================================================================
    def _install_dir_probe(self, game: Game, notes: list[str]) -> tuple[bool, bool]:
        """Resolve the abs install dir and return (sr_present, dlssg_present).

        Never raises -- a resolution failure / unreadable dir yields (False,
        False), and notes the failure so it is not indistinguishable from a
        clean "no DLLs here". ``nvapi64.dll`` is noted as an nvapi hint but not
        surfaced as a feature flag (the INTEGRATION spec calls it a hint).
        """
        try:
            abs_dir = self._resolve_install(game)
        except Exception as exc:  # noqa: BLE001 -- never raise
            if not any("install resolve failed" in n for n in notes):
                notes.append(f"install resolve failed ({exc.__class__.__name__})")
            abs_dir = None
        if not abs_dir:
            return False, False
        root = pathlib.Path(abs_dir)
        if not root.is_dir():
            return False, False
        return self._probe_dll_tree(root)

    # Well-known subdirs where NVIDIA DLSS DLLs nominally live. The eager pass
    # checks these first -- it resolves the common layouts (flat root, bin/,
    # the Unreal ``Binaries/Win64`` family, the NVIDIA third-party plugin dir)
    # without a full tree walk. NOTE: this must stay aligned with the candidate
    # set used by ``services.dlss_target_paths()`` for the DLSS-swap flow, or
    # the swap UI can target a DLL the capability chip still reports UNKNOWN --
    # the integrated-but-disjoint bug this tuple widening closes.
    _DLL_SUBDIRS: tuple[str, ...] = (
        "bin", "Binaries",
        "bin/x64", "win64",
        "Binaries/Win64", "Binaries/Win64Shipping",
        "Engine/Binaries/ThirdParty/NVIDIA",
    )

    def _probe_dll_tree(self, root: pathlib.Path) -> tuple[bool, bool]:
        """Walk a game's install tree for nvngx_dlss.dll / nvngx_dlssg.dll.

        Two passes: an eager pass over the well-known subdirs (where DLSS DLLs
        nominally live), then a bounded ``os.walk`` fallback so an UNLISTED game
        shipping its DLL at an exotic path still self-detects -- the whole point
        of the install-dir tier is that curation can't cover every shipping
        layout (e.g. a UE5 game with its own plugin vendoring nvngx_dlss.dll).
        Bounded: max depth 5, bulky media/archive subtrees pruned, fan-out cap,
        early-out the moment both DLL kinds are confirmed. Never raises.
        """
        sr = dlssg = False

        # ---- pass 1: well-known roots (the common layouts resolve here) ----
        cands = [root]
        for sub in self._DLL_SUBDIRS:
            cands.append(root / sub)
        for d in cands:
            if not d.is_dir():
                continue
            if not sr and (d / "nvngx_dlss.dll").is_file():
                sr = True
            if not dlssg and (d / "nvngx_dlssg.dll").is_file():
                dlssg = True
            if sr and dlssg:
                return sr, dlssg

        # ---- pass 2: bounded fallback walk (unlisted exotic layouts) ----
        try:
            sr, dlssg = self._bounded_dll_walk(root, sr, dlssg)
        except Exception:  # noqa: BLE001 -- the never-raise contract
            logger.debug("bounded dll-tree walk failed (ignored)", exc_info=True)
        return sr, dlssg

    def _bounded_dll_walk(
        self, root: pathlib.Path, sr: bool, dlssg: bool
    ) -> tuple[bool, bool]:
        """Bounded recursive search for DLSS DLLs, honoring never-raise.

        Caps runtime on big game trees (some installs exceed 50 GB with packed
        media archives): max depth 5, prune the bulky subtrees that only hold
        media/archive content (never code DLLs), cap per-dir fan-out, and
        early-out the instant both DLL kinds are confirmed.
        """
        max_depth = 5
        # Subtrees that only hold media/archive content -- searching them burns
        # I/O and never finds nvngx_dlss.dll, so prune up front.
        prune = {
            "content", "cinematics", "_commonredist", "redist",
            "data", "paks", "movies", "video", "videos", "audio",
            "precache", "shadercache", "logs", "screenshots",
        }
        # A single dir with >this many entries is almost certainly a packed
        # archive expansion or an asset dump; skip rather than fan out.
        max_entries = 4000

        for dirpath, dirnames, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, root)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth > max_depth:
                del dirnames[:]   # don't descend past the cap
                continue
            # prune media/archive subtrees in place (mutate dirnames so os.walk
            # skips them) and drop .pak/.pak.<n> archive files' parent dirs.
            dirnames[:] = [
                d for d in dirnames
                if d.lower() not in prune and not d.lower().endswith(".pak")
            ]
            if len(dirnames) > max_entries:
                del dirnames[:]
            if "nvngx_dlss.dll" in filenames and not sr:
                sr = True
            if "nvngx_dlssg.dll" in filenames and not dlssg:
                dlssg = True
            if sr and dlssg:
                break
        return sr, dlssg

    # ==================================================================
    # Tier 5 -- prefix DLL probe
    # ==================================================================
    def _prefix_probe(self, game: Game, notes: list[str]) -> tuple[bool, bool]:
        """Probe the Proton prefix system32 for DLSS DLLs. Never raises.

        Reuses the curated :data:`adapters.diagnostics._NVAPI_DLLS` name set
        rather than re-hardcoding the filenames, so the prefix probe and the
        diagnostics adapter agree on which DLLs count. The set membership
        also surfaces ``nvapi64.dll`` / ``nvapi.dll`` as an NVAPI hint (the
        spec calls them a hint, not a feature flag) -- present here only so a
        future caller can tell "DLSS DLL present but no NVAPI" from "DLSS +
        NVAPI wired", via the returned ``nvapi`` bool.
        """
        try:
            pfx = (
                pathlib.Path(self._steam_root) / "steamapps" / "compatdata"
                / str(game.appid) / "pfx" / "drive_c" / "windows" / "system32"
            )
        except Exception as exc:  # noqa: BLE001
            if not any("prefix probe failed" in n for n in notes):
                notes.append(f"prefix probe failed ({exc.__class__.__name__})")
            return False, False
        if not pfx.is_dir():
            if not any("prefix missing" in n for n in notes):
                notes.append("prefix missing")
            return False, False
        found = [d for d in _NVAPI_DLLS if (pfx / d).is_file()]
        sr = "nvngx_dlss.dll" in found
        dlssg = "nvngx_dlssg.dll" in found
        return sr, dlssg

    # ==================================================================
    # cache + fetch
    # ==================================================================
    def _fetch_or_cache(self, url: str, notes: list[str]) -> dict:
        """Return the parsed feature DB, refreshing the TTL cache when stale.

        On a fresh fetch, the new document overwrites the cache file. On a
        network/parse failure (stale OR no cache) the exception propagates to
        :meth:`_online_tier`, which degrades to a tier skip + a "community DB
        unreachable" note -- NOT a stale-as-fresh fallback. So this method may
        raise, but ``probe`` never does; and the user is never served a
        community-DB result silently labelled "online" without a fresh fetch.
        """
        cached = self._load_cache()
        if cached is not None and not self._cache_stale(cached):
            notes.append("online (cached)")
            return cached["payload"]
        # stale or absent -- attempt a refetch
        fresh = self._http_get_json(url)
        self._write_cache(fresh)
        notes.append("online")
        return fresh

    def _http_get_json(self, url: str) -> dict:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json",
                         "User-Agent": "nvidia-gui/feature-detection"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            raw = r.read()
        return json.loads(raw.decode("utf-8", errors="replace"))

    def _load_cache(self) -> dict | None:
        try:
            with open(_CACHE_PATH, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if not isinstance(cached, dict) or "payload" not in cached:
            return None
        if not isinstance(cached.get("fetched_at"), (int, float)):
            return None
        return cached

    def _write_cache(self, payload: dict) -> None:
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            doc = {"fetched_at": int(time.time()), "payload": payload}
            tmp = _CACHE_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            tmp.replace(_CACHE_PATH)
        except OSError as exc:
            logger.debug("feature cache write failed: %s", exc)

    @staticmethod
    def _cache_stale(cached: dict) -> bool:
        return (time.time() - float(cached.get("fetched_at", 0))) > _CACHE_TTL_S

    # ==================================================================
    # helpers
    # ==================================================================
    @staticmethod
    def _flag(value: bool, source: str) -> FeatureFlag:
        return FeatureFlag(bool(value), source)

    @staticmethod
    def _flag_unknown() -> FeatureFlag:
        return FeatureFlag(False, FeatureSource.UNKNOWN)

    @staticmethod
    def _default_note(
        dlss_sr: FeatureFlag, reflex: FeatureFlag, rt: FeatureFlag
    ) -> str:
        # Short provenance note when nothing notable happened.
        return f"offline -- sr:{dlss_sr.source} reflex:{reflex.source} rt:{rt.source}"
