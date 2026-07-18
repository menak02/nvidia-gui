"""Launch-injection into Steam's localconfig.vdf — idempotent, prepend-only.

We find ``userdata/<steamid>/config/localconfig.vdf``, parse it, and set the
``LaunchOptions`` value for each target appid so it begins with::

    source "<env-file>" 2>/dev/null; <existing user options> %command%

Key properties:

* **Prepend, never clobber.** Any user hand-tuning (WINEDLLOVERRIDES,
  PROTON_ENABLE_HDR, a custom exe path, …) is preserved AFTER the source line.
* **Idempotent.** Re-applying refreshes the path but does not stack duplicate
  ``source`` lines. Removing restores the original options verbatim.
* **Atomic.** Writes go to a .tmp then rename.
"""

from __future__ import annotations

import logging
import pathlib
import re

from ..application.ports import LaunchOptionPort
from .vdf_lite import VdfError, dumps, loads

logger = logging.getLogger(__name__)

# The exact managed prefix we prepend, and the regex that recognises *our* prior
# prefix on re-apply so we replace the path instead of stacking duplicates. We
# match only the `source "..." 2>/dev/null;` shape we emit. The env-file path
# always lives under XDG_CACHE_HOME with a numeric appid, so it never contains a
# quote or backslash — `[^"]+` is exact for our paths, and the regex is anchored
# on our own ` 2>/dev/null;` tail so a user's unrelated sh never collides. (Note
# the regex runs on the *parsed* LaunchOptions value, where escapes are already
# gone, so a `\"`-tolerant alternation here would be pure ceremony.)
#
# The leading `^` is load-bearing: without it, `re.sub` would strip *any*
# `source "..." 2>/dev/null;` token embedded deeper in a user's own options — a
# latent data-corruption bug. With `^` (and no re.MULTILINE) the match lands only
# at the head of the value, i.e. only our genuine managed prefix is ever touched.
_SOURCE_PREFIX = re.compile(r'^source "[^"]+" 2>/dev/null; ?')


def _build_owned(env_file_abs: str) -> str:
    """The managed prefix we prepend. Double-quote-escaped for VDF."""
    inner = env_file_abs.replace("\\", "\\\\").replace('"', '\\"')
    return f'source "{inner}" 2>/dev/null; '


class SteamLaunchOptions(LaunchOptionPort):
    def __init__(self, steam_root: pathlib.Path | str | None = None) -> None:
        self._userdata = (
            pathlib.Path(steam_root or (pathlib.Path.home() / ".local/share/Steam"))
            / "userdata"
        )

    def _find_localconfig(self) -> tuple[int, pathlib.Path] | None:
        if not self._userdata.is_dir():
            return None
        # newest-by-mtime is better than lowest id (more recent login wins)
        cands = sorted(
            self._userdata.glob("*/config/localconfig.vdf"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in cands:
            try:
                idx = int(p.parent.parent.name)
            except ValueError:
                continue
            return idx, p
        return None

    # ------------------------------------------------------------------
    def _read_vdf(self, path: pathlib.Path) -> dict | None:
        try:
            return loads(path.read_text(encoding="utf-8", errors="replace"))
        except (VdfError, OSError) as exc:
            logger.error("localconfig unreadable: %s", exc)
            return None

    def _atomic_write_vdf(self, path: pathlib.Path, data: dict) -> None:
        # Stage beside the target (same dir ⇒ same filesystem ⇒ atomic rename),
        # using the file's own name + ".tmp" so concurrent calls never collide on
        # a shared temp name. Clean the temp if the write or rename fails.
        tmp = path.parent / (path.name + ".tmp")
        try:
            tmp.write_text(dumps(data), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def ensure_wrapper(self, appid: str, env_file_abs: str) -> bool:
        found = self._find_localconfig()
        if not found:
            logger.warning("no localconfig.vdf under %s", self._userdata)
            return False
        _uid, path = found
        data = self._read_vdf(path)
        if data is None:
            return False

        apps = self._apps(data)
        cfg = apps.setdefault(str(appid), {})
        if not isinstance(cfg, dict):
            apps[str(appid)] = cfg = {}
        existing = cfg.get("LaunchOptions", "")
        existing = "" if existing is None else str(existing)
        # strip any prior managed prefix (idempotent refresh of the path)
        user_part = _SOURCE_PREFIX.sub("", existing).strip()
        
        # Clean curated variables from user part to prevent command line overrides
        words = user_part.split()
        cleaned_words = []
        found_cmd = False
        curated_keys = {
            "VKD3D_CONFIG", "PROTON_ENABLE_NVAPI", "PROTON_HIDE_NVIDIA_GPU", "DXVK_ENABLE_NVAPI",
            "DXVK_NVAPI_DRS_NGX_DLSS_FG_OVERRIDE",
            "DXVK_NVAPI_DRS_NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION",
            "DXVK_NVAPI_DRS_NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION",
            "DXVK_NVAPI_DRS_NGX_DLSSG_MODE",
            "DXVK_NVAPI_DRS_NGX_DLSSG_MULTI_FRAME_COUNT"
        }
        for word in words:
            if not found_cmd:
                if word == "%command%":
                    found_cmd = True
                    cleaned_words.append(word)
                elif "=" in word and not word.startswith("-"):
                    key, _, val = word.partition("=")
                    if key in curated_keys:
                        continue
                    cleaned_words.append(word)
                else:
                    cleaned_words.append(word)
            else:
                cleaned_words.append(word)
        user_part = " ".join(cleaned_words)

        owned = _build_owned(env_file_abs)
        new_val = (owned + (" " + user_part if user_part else "")).strip()
        cfg["LaunchOptions"] = new_val
        self._atomic_write_vdf(path, data)
        logger.info("stamped wrapper for %s into %s", appid, path)
        return True

    def remove_wrapper(self, appid: str) -> bool:
        found = self._find_localconfig()
        if not found:
            return False
        _uid, path = found
        data = self._read_vdf(path)
        if data is None:
            return False
        apps = self._apps(data)
        cfg = apps.get(str(appid))
        if not isinstance(cfg, dict) or "LaunchOptions" not in cfg:
            return True
        cleaned = _SOURCE_PREFIX.sub("", str(cfg["LaunchOptions"])).strip()
        if cleaned:
            cfg["LaunchOptions"] = cleaned
        else:
            del cfg["LaunchOptions"]
            if not cfg:
                apps.pop(str(appid), None)
        self._atomic_write_vdf(path, data)
        return True

    def get_raw_options(self, appid: str) -> str:
        found = self._find_localconfig()
        if not found:
            return ""
        _uid, path = found
        data = self._read_vdf(path)
        if data is None:
            return ""
        apps = self._apps(data)
        cfg = apps.get(str(appid))
        if not isinstance(cfg, dict):
            return ""
        return str(cfg.get("LaunchOptions", ""))

    @staticmethod
    def _apps(data: dict) -> dict:
        """Navigate to the Steam apps tree, creating container blocks as needed.

        The VDF depth under userdata localconfig is:
            UserLocalConfigStore.Software.Valve.Steam.apps
        """
        cur: dict = data
        for key in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps"):
            nxt = cur.get(key)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[key] = nxt
            cur = nxt
        return cur
