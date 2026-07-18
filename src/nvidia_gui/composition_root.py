"""Composition root — instantiate concrete adapters and wire them into UseCases.

Every concrete choice (nvidia-smi for telemetry, TOML for config, the
filesystem for DLL swaps, the IPC server) is made here, in one place, so the
rest of the app stays adapter-agnostic. Tests substitute the UseCases' ports
with fakes here; the presentation layer imports nothing from ``adapters/``.
"""

from __future__ import annotations

import logging
import pathlib

from .adapters.config_toml import get_config_manager
from .adapters.diagnostics import SteamDiagnostics
from .adapters.dlss_cache_fs import FsDlssCache
from .adapters.dlss_swap_fs import FsDlssSwap
from .adapters.feature_detection import SteamFeatureDetector
from .adapters.game_library_steam import SteamLibrary
from .adapters.gpu_query_sm import NvidiaSmiGpu
from .adapters.ipc_socket import IpcServer
from .adapters.kernel_param_modprobe import ModprobeKernelParam
from .adapters.launch_option_steam import SteamLaunchOptions
from .adapters.profile_store_fs import FsProfileStore
from .adapters.system_info import EnvDisplayInfo, SmiDriverInfo
from .adapters.vibrance_nvibrant import NvibrantVibrance
from .application.use_cases import UseCases
from .domain.models import Game

logger = logging.getLogger(__name__)


def _install_abs(games: SteamLibrary, game: Game) -> str | None:
    """Resolve a game's absolute install dir from the Steam library."""
    p = games.install_path(game.installdir)
    return str(p) if p else None


def _wire(cfg) -> UseCases:
    paths = cfg.paths
    # Honour an explicit override; otherwise default to the real XDG Steam root
    # (the same default SteamLibrary uses). An earlier fallback computed
    # ``~/Steam`` from the config dir's grandparent — a path that does not
    # exist — so a config.toml WITHOUT a [paths] section silently scanned an
    # empty library and the Games page reported "no games found".
    steam_root = cfg.get("paths.steam_root") or str(pathlib.Path.home() / ".local/share/Steam")
    games = SteamLibrary(steam_root)
    settings = get_config_manager()  # ConfigManager now subclasses SettingsPort
    return UseCases(
        gpu=NvidiaSmiGpu(),
        driver=SmiDriverInfo(),
        display=EnvDisplayInfo(),
        games=games,
        profiles=FsProfileStore(paths.env_dir, paths.profiles),
        launch=SteamLaunchOptions(steam_root),
        dlss_cache=FsDlssCache(paths.dlss_cache),
        # the swap adapter never imports a peer adapter; it resolves installs
        # via this closure over the library we already built.
        dlss_swap=FsDlssSwap(resolve_install=lambda g: _install_abs(games, g)),
        kernel=ModprobeKernelParam(),
        diagnostics=SteamDiagnostics(steam_root),
        detection=SteamFeatureDetector(
            resolve_install=lambda g: _install_abs(games, g),
            steam_root=steam_root,
            settings=settings,
            bundled_db=pathlib.Path(__file__).parent / "assets" / "nvidia_features.toml",
        ),
        settings=settings,
        vibrance=NvibrantVibrance(settings=settings),
    )


def build_use_cases() -> UseCases:
    """Use cases wired against the default (live) adapters."""
    return _wire(get_config_manager())


def build_ipc(uc: UseCases, socket_path: str) -> IpcServer:
    """The JSON-over-AF_UNIX control socket — liveness + telemetry for a CLI."""
    def handler(req: dict) -> dict:
        cmd = (req or {}).get("cmd") or (req or {}).get("raw", "")
        if cmd in ("ping", "liveness"):
            return {"ok": True, "cmd": cmd}
        if cmd == "snapshot":
            try:
                s = uc.snapshot()
                return {"gpu": s.gpu_name, "driver": s.driver_version,
                        "temp": s.temperature_c, "util": s.gpu_util_pct}
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc)}
        if cmd == "games":
            return {"games": [{"appid": g.appid, "name": g.name}
                              for g in uc.scan_games()]}
        return {"ok": True, "cmd": cmd, "note": "no-op"}

    return IpcServer(socket_path, handler)


def build_app() -> tuple[UseCases, IpcServer]:
    """Construct use cases + the IPC server bound to the app's socket path.

    One call from the GTK Application entry point; everything concrete is
    resolved here so the presentation layer stays free of adapter imports.
    """
    cfg = get_config_manager()
    uc = _wire(cfg)
    ipc = build_ipc(uc, str(cfg.paths.socket))
    return uc, ipc
