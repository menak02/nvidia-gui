"""Per-game profile persistence: our own TOML + the sourced shell env-file."""

from __future__ import annotations

import logging
import pathlib

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from ..application.ports import ProfileStorePort
from ..domain import services
from ..domain.models import DlssPreset, GameProfile
from .config_toml import _dump_toml  # reuse the minimal serializer

logger = logging.getLogger(__name__)

# Profile TOML is a single [profile] section mapping our GameProfile fields.


def _atomic_write(target: pathlib.Path, text: str) -> None:
    """Write ``text`` to a sibling ``.tmp`` then rename onto ``target`` (atomic
    on the same filesystem), unlinking the temp if the write or rename fails so
    no stale ``.tmp`` litters the profile/env dir."""
    tmp = target.parent / (target.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


# Fields persisted to the [profile] TOML section. This tuple is BOTH the
# save-whitelist and the load-allowlist: dropping a field here (e.g. the proven
# no-op `enable_low_latency_vk`) silently drops it from on-disk TOML on the next
# read→save cycle — a field-by-field schema migration with no version header.
_FIELDS = (
    "appid", "expose_nvidia_gpu", "enable_nvapi", "enable_dxvk_nvapi",
    "enable_rtx", "force_dxr", "dlss_preset", "enable_dlss_fg",
    "dlss_fg_preset", "dlss_fg_mode", "dlss_fg_multiplier",
    "enable_reflex", "enable_gamemode", "enable_mangohud",
    "dlss_dll_swap", "dlss_swap_version",
)


class FsProfileStore(ProfileStorePort):
    """One TOML + one sourced env file per appid under XDG cache."""

    def __init__(self, env_dir: pathlib.Path, profile_dir: pathlib.Path) -> None:
        self._env_dir = env_dir
        self._profile_dir = profile_dir
        self._env_dir.mkdir(parents=True, exist_ok=True)
        self._profile_dir.mkdir(parents=True, exist_ok=True)

    def load(self, appid: str) -> GameProfile:
        f = self._profile_dir / f"{appid}.toml"
        if not f.is_file():
            return GameProfile(appid=appid)
        try:
            with f.open("rb") as fh:
                raw = tomllib.load(fh)
        except Exception as exc:  # noqa: BLE001
            logger.warning("profile %s unreadable: %s", appid, exc)
            return GameProfile(appid=appid)
        sec = raw.get("profile", {}) if isinstance(raw, dict) else {}
        kwargs = {"appid": appid}
        for field in _FIELDS:
            if field == "appid":
                continue
            if field in sec:
                kwargs[field] = sec[field]
        # extra_env stored as [profile.extra_env] sub-table
        if isinstance(sec.get("extra_env"), dict):
            kwargs["extra_env"] = {str(k): str(v) for k, v in sec["extra_env"].items()}
        return GameProfile(**kwargs)

    def save(self, profile: GameProfile) -> None:
        # 1) TOML profile
        sec: dict = {}
        for field in _FIELDS:
            if field == "appid":
                continue
            val = getattr(profile, field)
            if val is None:
                continue
            sec[field] = val
        if profile.extra_env:
            sec["extra_env"] = dict(profile.extra_env)
        body = _dump_toml({"profile": sec})
        _atomic_write(self._profile_dir / f"{profile.appid}.toml", body)

        # 2) sourced env file (POSIX sh) — always rewritten from the profile
        env_body = services.render_env_file(profile)
        _atomic_write(self._env_dir / f"{profile.appid}.env", env_body)

    def env_file_path(self, appid: str) -> str:
        return str(self._env_dir / f"{appid}.env")

    def has(self, appid: str) -> bool:
        """True iff a managed profile TOML exists on disk for *appid*."""
        return (self._profile_dir / f"{appid}.toml").is_file()
