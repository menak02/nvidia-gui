"""Use cases — the operations the UI actually performs.

Each method maps to a user action ("scan games", "save this game's profile",
"apply global settings", "swap DLSS"). They orchestrate ports and run domain
rules; they contain *no* adapter specifics. The composition root injects the
concrete ports.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from ..domain import services
from ..domain.models import (
    DiagnosticReport,
    DlssBundle,
    DisplayInfo,
    DriverInfo,
    Game,
    GameCapability,
    GameProfile,
    GlobalDriverSettings,
    GpuSnapshot,
    PersistenceState,
)
from .ports import (
    DiagnosticsPort,
    DlssCachePort,
    DlssSwapPort,
    DisplayInfoPort,
    DriverInfoPort,
    FeatureDetectionPort,
    GpuQueryPort,
    GameLibraryPort,
    KernelParamPort,
    LaunchOptionPort,
    ProfileStorePort,
    SettingsPort,
)


@dataclass
class SaveProfileResult:
    ok: bool
    env_file: str
    wrapper_written: bool
    message: str


@dataclass
class SwapResult:
    ok: bool
    written: list[str]
    backups: list[str]
    message: str


class UseCases:
    """Application service facade. Constructed once at composition root."""

    def __init__(
        self,
        *,
        gpu: GpuQueryPort,
        driver: DriverInfoPort,
        display: DisplayInfoPort,
        games: GameLibraryPort,
        profiles: ProfileStorePort,
        launch: LaunchOptionPort,
        dlss_cache: DlssCachePort,
        dlss_swap: DlssSwapPort,
        kernel: KernelParamPort,
        diagnostics: DiagnosticsPort,
        detection: FeatureDetectionPort | None = None,
        settings: SettingsPort | None = None,
    ) -> None:
        self.gpu = gpu
        self.driver = driver
        self.display = display
        self.games = games
        self.profiles = profiles
        self.launch = launch
        self.dlss_cache = dlss_cache
        self.dlss_swap = dlss_swap
        self.kernel = kernel
        self.diagnostics = diagnostics
        # Optional: per-game feature-detection (capability probe). Defaulted so a
        # test constructing UseCases with the functional ports still works.
        self.detection = detection
        # Optional: presentation prefs (window/pane geometry). Defaulted so a
        # test constructing UseCases with the 10 functional ports still works.
        self.settings = settings

    # ---- telemetry --------------------------------------------------------
    def snapshot(self) -> GpuSnapshot:
        return self.gpu.snapshot()

    def driver_info(self) -> DriverInfo:
        return self.driver.read()

    def display_info(self) -> DisplayInfo:
        return self.display.read()

    # ---- games ------------------------------------------------------------
    def scan_games(self) -> list[Game]:
        return self.games.scan()

    def get_profile(self, appid: str) -> GameProfile:
        return self.profiles.load(appid)

    def save_profile(self, profile: GameProfile) -> SaveProfileResult:
        """Persist the TOML profile, render+write the env-file, and ensure the
        wrapper line is stamped (so the env vars actually take effect)."""
        try:
            self.profiles.save(profile)
        except OSError as exc:
            return SaveProfileResult(False, "", False, f"profile save failed: {exc}")

        env_file = self.profiles.env_file_path(profile.appid)
        wrapper_written = False
        try:
            wrapper_written = self.launch.ensure_wrapper(profile.appid, env_file)
        except OSError as exc:
            return SaveProfileResult(
                False, env_file, False, f"wrapper stamp failed: {exc}"
            )

        # `ok` is tied to `wrapper_written`: the env file is only effective if the
        # `source "<env>" 2>/dev/null;` prefix was actually stamped into
        # localconfig.vdf. A saved profile whose wrapper silently didn't land is a
        # failure the user must see — otherwise they'd see "saved" while the
        # game runs with the wrong (or no) env, which was the Cyberpunk bug.
        if wrapper_written:
            note = ("Profile + env file written; launch wrapper stamped into "
                    "localconfig.vdf — env will take effect on next launch.")
        else:
            note = ("Profile + env file written, but the launch wrapper was NOT "
                    "written (Steam localconfig.vdf unreadable?) — the env file "
                    "will NOT be sourced at launch, so these toggles take no effect.")
        return SaveProfileResult(wrapper_written, env_file, wrapper_written, note)

    def optimize_for_rig(self, game: Game) -> SaveProfileResult:
        """Mirror the NVIDIA App's 'Optimize for this rig': read detected
        hardware, derive a recommended launch-env profile, overlay its graphics
        / RT / DLSS / latency fields onto the existing profile (preserving the
        DLL swap, extra_env, and MangoHud — Optimize tunes *graphics*, it
        doesn't wipe advanced choices), and save. Returns the honest
        :class:`SaveProfileResult`."""
        try:
            gpu = self.gpu.snapshot()
            driver = self.driver.read()
            display = self.display.read()
        except Exception as exc:  # noqa: BLE001 — a hardware-read failure is honest
            return SaveProfileResult(False, "", False, f"hardware read failed: {exc}")
        rec = services.recommend_optimal_profile(game, gpu, driver, display)
        existing = self.profiles.load(game.appid)
        merged = replace(
            existing,
            expose_nvidia_gpu=rec.expose_nvidia_gpu,
            enable_nvapi=rec.enable_nvapi,
            enable_dxvk_nvapi=rec.enable_dxvk_nvapi,
            enable_rtx=rec.enable_rtx,
            force_dxr=rec.force_dxr,
            dlss_preset=rec.dlss_preset,
            enable_dlss_fg=rec.enable_dlss_fg,
            enable_reflex=rec.enable_reflex,
            enable_gamemode=rec.enable_gamemode,
            # MangoHud (overlay/debug) + dlss_dll_swap + extra_env preserved as-is.
        )
        return self.save_profile(merged)

    def run_diagnostics(self, game: Game) -> DiagnosticReport:
        """Read-only per-game health probe (Vulkan ICD/RT, Proton tool, prefix
        NVAPI/DLSS DLLs, prefix age) + a most-likely-cause verdict. Runs
        subprocess/filesystem probes — the view should call this off the main
        loop, same as the Streamline download worker."""
        return self.diagnostics.probe(game)

    # ---- per-game feature detection (capability, not applied toggles) -------
    def detect_game_features(self, game: Game) -> GameCapability:
        """Probe a game's NVIDIA-feature support (DLSS-SR / Frame Gen / Reflex /
        RT). Routes through the detection port so views import no adapter.
        Offline-graceful: the adapter never raises; an undecided feature reads
        :data:`~..domain.models.FeatureSource.UNKNOWN`. Returns an all-UNKNOWN
        capability when no detection port is wired (e.g. a unit test)."""
        if self.detection is None:
            return GameCapability(appid=game.appid)
        return self.detection.probe(game)

    def feature_override(self, game: Game, kind: str) -> bool | None:
        """Read a user-set feature override, or None if unset/`auto`. The UI uses
        this to badge a toggle as 'overridden by you' (the detection said one
        thing; you forced the other). ``kind`` is one of dlss_sr/dlss_fg/reflex/rt."""
        if self.settings is None:
            return None
        v = self.settings.get(f"feature.{game.appid}.{kind}", None)
        if v is None or v == "auto":
            return None
        return bool(v)

    def set_feature_override(self, game: Game, kind: str, supported: bool) -> None:
        """Persist a manual override: force a feature to supported/unsupported
        regardless of detection. Lives in app-config (``feature.<appid>.<kind>``)
        so it survives reopen — it is NOT a launch env var; it only governs UI
        gating and what Optimize recommends. No-op when no settings port wired."""
        if self.settings is None:
            return
        self.settings.set(f"feature.{game.appid}.{kind}", bool(supported))

    def clear_feature_override(self, game: Game, kind: str) -> None:
        """Drop an override. The minimal TOML store has no delete-key path, so we
        write the ``'auto'`` sentinel that :meth:`feature_override` treats as
        'no override' — the detection pipeline then decides the feature again."""
        if self.settings is None:
            return
        self.settings.set(f"feature.{game.appid}.{kind}", "auto")

    def remove_wrapper(self, appid: str) -> bool:
        try:
            return self.launch.remove_wrapper(appid)
        except OSError:
            return False

    # ---- profile summary (presented on the Profiles page) ----------------
    def profile_flags(self, profile: GameProfile) -> list[str]:
        """Human-readable list of what's ON in a profile (DLSS preset/swap/...).

        Routes the domain rule through the use-case layer so views import no
        domain service directly."""
        return services.summarise_toggles(profile)

    # ---- DLSS -------------------------------------------------------------
    def list_dlss_versions(self):
        return self.dlss_cache.list_versions()

    def seed_dlss_from(self, source_dir: str, version_label: str):
        return self.dlss_cache.seed_from(source_dir, version_label)

    def download_streamline_release(self, progress=None):
        """Fetch the latest Streamline GitHub release into the DLSS cache.

        Blocking — the view runs it on a worker thread and mirrors progress
        onto the GTK main loop. Power users with a local SDK checkout can call
        ``seed_dlss_from`` instead; the normal flow is this download.
        """
        return self.dlss_cache.download_latest_release(progress)

    def dlss_swap_status(self, game: Game, prefix_system32: str | None):
        return self.dlss_swap.status(game, prefix_system32)

    def apply_dlss_swap(
        self, game: Game, version: str, prefix_system32: str | None
    ) -> SwapResult:
        bundle = self.dlss_cache.get_bundle(version)
        if bundle is None:
            return SwapResult(False, [], [], f"DLSS version {version} not in cache")
        written, backups = self.dlss_swap.apply(game, bundle, prefix_system32)
        if not written:
            return SwapResult(False, [], [], "no DLL targets found/ written")
        return SwapResult(True, written, backups, f"swapped {len(written)} DLL(s)")

    def revert_dlss_swap(self, game: Game, prefix_system32: str | None) -> SwapResult:
        restored = self.dlss_swap.revert(game, prefix_system32)
        if not restored:
            return SwapResult(False, [], [], "no backups to restore")
        return SwapResult(True, restored, [], f"restored {len(restored)} DLL(s)")

    # ---- global driver ----------------------------------------------------
    def get_global_settings(self) -> GlobalDriverSettings:
        return self.kernel.read_config()

    def apply_global_settings(self, settings: GlobalDriverSettings):
        return self.kernel.write_config(settings)

    def kernel_write_status(self) -> bool:
        return self.kernel.can_write_without_root()

    def persistence_status(self) -> PersistenceState:
        """Live persistence-mode state — survives-reboot (service enabled) vs
        running-now vs the GPU's current flag. Read-only, no main-loop hop."""
        return self.kernel.persistence_status()

    def set_persistence(self, enabled: bool) -> tuple[bool, str]:
        """Enable/disable the nvidia-persistenced service. *Unlike* the modprobe
        knobs this takes effect immediately (services start/stop now) and
        survives reboot via the service's enabled state. Needs root — the
        adapter handles the pkexec/sudo prompt; the view should free it as a
        quick action, not block on it."""
        return self.kernel.set_persistence(enabled)

    # ---- app prefs (presentation-facing persisted settings) --------------
    def setting(self, dotted: str, default=None):
        """Read an app preference (window/pane geometry, ...). Returns *default*
        when no settings port is wired (e.g. a unit test) so the view always
        gets a value."""
        if self.settings is None:
            return default
        return self.settings.get(dotted, default)

    def set_setting(self, dotted: str, value) -> None:
        """Persist an app preference. No-op when no settings port is wired."""
        if self.settings is None:
            return
        self.settings.set(dotted, value)
