"""XDG TOML config store — the app's own settings (not per-game profiles).

Reads via stdlib :mod:`tomllib`; writes via a tiny internal serializer so we
depend on *nothing* beyond the stdlib and PyGObject. The serializer only needs
to round-trip the flat-ish dict we control (sections of scalars/lists), so it
is deliberately minimal.
"""

from __future__ import annotations

import os
import pathlib
import threading
from typing import Any

try:
    import tomllib  # py 3.11+
except ImportError:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib  # type: ignore

from ..application.ports import SettingsPort

__all__ = [
    "XDGPaths",
    "ConfigManager",
    "get_config_manager",
]


def _xdg(env: str, *sub: str) -> pathlib.Path:
    base = os.environ.get(env) or str(pathlib.Path.home() / {
        "XDG_DATA_HOME": ".local/share",
        "XDG_CONFIG_HOME": ".config",
        "XDG_CACHE_HOME": ".cache",
    }[env])
    return pathlib.Path(base).expanduser().joinpath(*sub)


class XDGPaths:
    """Resolved application base directories (XDG-compliant, with fallbacks)."""

    APP = "nvidia-gui"

    def __init__(self) -> None:
        self.data = _xdg("XDG_DATA_HOME", self.APP)
        self.config = _xdg("XDG_CONFIG_HOME", self.APP)
        self.cache = _xdg("XDG_CACHE_HOME", self.APP)
        # well-under the 108-byte AF_UNIX limit
        self.socket = (self.cache / "nvidia-gui.sock").resolve()
        self.profiles = self.cache / "profiles"
        self.env_dir = self.cache / "env"
        self.dlss_cache = self.data / "dlss_cache"
        self.config_toml = self.data / "config.toml"
        self.modprobe_conf = pathlib.Path("/etc/modprobe.d/nvidia-gui.conf")
        for p in (self.data, self.config, self.cache, self.profiles, self.env_dir):
            p.mkdir(parents=True, exist_ok=True)
        # dlss cache created lazily

    def game_env_file(self, appid: str) -> pathlib.Path:
        return self.env_dir / f"{appid}.env"

    def game_profile_file(self, appid: str) -> pathlib.Path:
        return self.profiles / f"{appid}.toml"


class ConfigManager(SettingsPort):
    """Singleton, thread-safe, lazy TOML key store (dotted-path access).

    Subclasses :class:`SettingsPort` so the presentation layer can persist
    app-prefs (window/pane geometry) through the use-case facade without the
    view ever touching the concrete TOML adapter — the hexagonal seam. Its
    existing ``get``/``set`` methods already satisfy the port's abstractmethods.
    """

    _instance: "ConfigManager | None" = None
    _lock = threading.Lock()
    paths: XDGPaths

    def __new__(cls: type["ConfigManager"]) -> "ConfigManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        # Guard the whole init under the class lock so two threads that both
        # received the cached instance from __new__ can't race the _ready flag
        # and double-run _load/_write. Same Lock __new__ already uses; the calls
        # are sequential on a given thread, never nested, so no deadlock.
        with type(self)._lock:
            if getattr(self, "_ready", False):
                return
            self.paths = XDGPaths()
            self._data: dict[str, Any] = {}
            self._rlock = threading.RLock()
            self._load()
            self._ready = True

    # public API -----------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        with self._rlock:
            cur: Any = self._data
            for part in dotted.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    return default
                cur = cur[part]
            return cur

    def set(self, dotted: str, value: Any) -> None:
        with self._rlock:
            *parts, leaf = dotted.split(".")
            cur = self._data
            for part in parts:
                cur = cur.setdefault(part, {})
            cur[leaf] = value
            self._write()

    def reload(self) -> bool:
        try:
            self._load()
            return True
        except Exception:
            return False

    # internals ------------------------------------------------------------
    def _load(self) -> None:
        f = self.paths.config_toml
        if not f.is_file():
            self._data = self._defaults()
            self._write()
            return
        with f.open("rb") as fh:
            raw = tomllib.load(fh)
        self._data = raw if isinstance(raw, dict) else self._defaults()

    def _defaults(self) -> dict[str, Any]:
        return {
            "app": {"name": "NVIDIA-GUI", "version": "1.0.0"},
            "ui": {"theme": "nvidia-dark", "accent": "#76b900"},
            "telemetry": {"poll_seconds": 1.5},
            "dlss": {"default_swap_version": "", "auto_update": False,
                     "streamline_repo": "NVIDIA-RTX/Streamline"},
            "nvapi": {"enabled_default": True},
            "feature_detection": {
                "online_enabled": True,
            },
            # Persisted window/pane geometry so a draggable Gtk.Paned divider and
            # the window size survive across launches (and a tiling WM that
            # overrides default-size at runtime doesn't fight a stale floor).
            "window": {"sidebar_width": 220, "width": 1180, "height": 760},
            "games": {"list_width": 300},
            "paths": {
                "steam_root": str(pathlib.Path.home() / ".local/share/Steam"),
            },
        }

    def _write(self) -> None:
        self.paths.data.mkdir(parents=True, exist_ok=True)
        tmp = self.paths.config_toml.parent / (self.paths.config_toml.name + ".tmp")
        try:
            tmp.write_text(_dump_toml(self._data), encoding="utf-8")
            tmp.replace(self.paths.config_toml)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise


# ---------------------------------------------------------------------------
#  Minimal TOML serializer (flat dicts of scalars/lists/strings/bools/None)
# ---------------------------------------------------------------------------
def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if v is None:
        # standard TOML has no null; skip (caller filters section writes)
        raise ValueError("None is not TOML-serialisable")
    if isinstance(v, str):
        if v == "":
            return '""'
        # bare-key-safe + basic-string fallback
        return f'"{_toml_escape(v)}"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"unsupported TOML type: {type(v).__name__}")


def _toml_escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\").replace('"', '\\"')
         .replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    )


def _dump_toml(data: dict[str, Any]) -> str:
    """Serialize a nestable dict (sections of scalars/lists + sub-tables) to TOML.

    A one-level table was always enough for the flat app config; the per-game
    profile store reuses this to write ``[profile]`` AND its ``extra_env``
    sub-map, which needs a real ``[profile.extra_env]`` table. So this recurses:
    within each table the scalar keys emit first, then sub-tables, so the output
    is always valid TOML (a parent's scalars cannot follow a child header).
    """
    out: list[str] = ["# Managed by nvidia-gui\n",
                      "# Edit by hand; the app rewrites on save.\n\n"]
    _emit_toml(out, data, prefix="")
    return "".join(out)


def _emit_toml(out: list[str], table: dict[str, Any], prefix: str) -> None:
    # two passes keep parent scalars ahead of any [parent.child] header
    scalars = [(k, v) for k, v in table.items() if not isinstance(v, dict)]
    subtables = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    for key, val in scalars:
        if val is None:
            continue  # skip nulls (TOML has none)
        try:
            out.append(f"{key} = {_toml_value(val)}\n")
        except (ValueError, TypeError):
            continue  # skip anything we can't serialise cleanly
    for key, val in subtables:
        section = f"{prefix}.{key}" if prefix else key
        out.append(f"[{section}]\n")
        _emit_toml(out, val, section)
        out.append("\n")


_get_mgr: ConfigManager | None = None


def get_config_manager() -> ConfigManager:
    global _get_mgr
    if _get_mgr is None:
        _get_mgr = ConfigManager()
    return _get_mgr
