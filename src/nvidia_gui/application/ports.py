"""Port interfaces — what the application needs from the outside world.

Implemented by adapters; consumed by use cases. Kept as Protocol/ABC so a test
can substitute a fake without subclassing a concrete adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Iterable

from ..domain.models import (
    DiagnosticReport,
    DisplayInfo,
    DlssBundle,
    DlssVersion,
    DriverInfo,
    Game,
    GameCapability,
    GameProfile,
    GlobalDriverSettings,
    GpuSnapshot,
    PersistenceState,
)


# ---------------------------------------------------------------------------
#  Telemetry / status
# ---------------------------------------------------------------------------
class GpuQueryPort(ABC):
    """Live GPU telemetry — typically nvidia-smi."""

    @abstractmethod
    def snapshot(self) -> GpuSnapshot:
        """One synchronous poll. Called off the GTK main loop."""


class DriverInfoPort(ABC):
    """Static driver/module facts gathered once per session."""

    @abstractmethod
    def read(self) -> DriverInfo:
        ...


class DisplayInfoPort(ABC):
    """Read-only display/compositor facts."""
    @abstractmethod
    def read(self) -> DisplayInfo:
        ...


# ---------------------------------------------------------------------------
#  Game library
# ---------------------------------------------------------------------------
class GameLibraryPort(ABC):
    """Scan installed games across launchers."""

    @abstractmethod
    def scan(self) -> list[Game]:
        ...


# ---------------------------------------------------------------------------
#  Per-game profile + launch injection
# ---------------------------------------------------------------------------
class ProfileStorePort(ABC):
    """Persist+load per-game profiles (our own TOML) and the sourced env-file."""

    @abstractmethod
    def load(self, appid: str) -> GameProfile:
        ...

    @abstractmethod
    def save(self, profile: GameProfile) -> None:
        ...

    @abstractmethod
    def env_file_path(self, appid: str) -> str:
        """Absolute path to the sourced env-file (for the wrapper line)."""
        ...

    @abstractmethod
    def has(self, appid: str) -> bool:
        """True iff a managed profile TOML exists on disk for *appid*.

        The at-a-glance 'this game already has a profile' signal, read WITHOUT
        loading+deserialising the whole profile (the badge/list just needs the
        boolean, not the fields). Presentation reaches this through
        :meth:`UseCases.has_profile` so it never stats the adapter's files
        directly (hexagonal: presentation depends on use cases, never adapters).
        """


class LaunchOptionPort(ABC):
    """Idempotently stamp the wrapper line into a launcher's launch-options."""

    @abstractmethod
    def ensure_wrapper(self, appid: str, env_file_abs: str) -> bool:
        """Return True if the wrapper was written/refreshed, False on failure."""

    @abstractmethod
    def remove_wrapper(self, appid: str) -> bool:
        ...


# ---------------------------------------------------------------------------
#  DLSS cache + swap
# ---------------------------------------------------------------------------
class DlssCachePort(ABC):
    """Versioned cache of Streamline DLLs.

    Populated either by :meth:`download_latest_release` (fetches the newest
    Streamline GitHub release and extracts only the known DLLs) or, for power
    users with a local SDK checkout, by :meth:`seed_from`.
    """

    @abstractmethod
    def list_versions(self) -> list[DlssVersion]:
        ...

    @abstractmethod
    def get_bundle(self, version: str) -> DlssBundle | None:
        ...

    @abstractmethod
    def seed_from(self, source_dir: str, version_label: str) -> DlssVersion:
        """Import/refresh one version from a local Streamline SDK bin dir.

        Power-user escape hatch for an existing on-disk SDK; the normal flow
        is :meth:`download_latest_release`.
        """

    @abstractmethod
    def download_latest_release(
        self, progress: Callable[[int, int], None] | None = None
    ) -> DlssVersion:
        """Fetch the latest Streamline release from GitHub and extract the
        known DLLs into the cache. ``progress(downloaded, total)`` is called
        periodically on the worker thread. Blocking — call off the main loop.
        """

    @abstractmethod
    def cache_dir(self) -> str:
        ...


class DlssSwapPort(ABC):
    """Copy a cached DLL bundle into a game's dirs (+ optional prefix), with
    backups so a swap is reversible."""

    @abstractmethod
    def apply(
        self,
        game: Game,
        bundle: DlssBundle,
        prefix_system32_abs: str | None,
    ) -> tuple[list[str], list[str]]:
        """Return (written_paths, backup_paths). Empty on failure."""

    @abstractmethod
    def revert(self, game: Game, prefix_system32_abs: str | None) -> list[str]:
        """Restore backups for this game; return restored path list."""

    @abstractmethod
    def status(self, game: Game, prefix_system32_abs: str | None) -> dict[str, str]:
        """kind -> 'ours'/'backed-up'/'missing' for each DLL kind."""


# ---------------------------------------------------------------------------
#  Global driver settings (kernel module params)
# ---------------------------------------------------------------------------
class KernelParamPort(ABC):
    """Write persistent NVreg_* tuning to modprobe.d (needs root), plus a
    best-effort runtime sysfs path where writable. Also owns runtime
    *persistence mode* (the nvidia-persistenced service): unlike the modprobe
    knobs, persistence is a running daemon, not a load-time option, so it
    takes effect immediately and survives reboot via the service state."""

    @abstractmethod
    def read_config(self) -> GlobalDriverSettings:
        ...

    @abstractmethod
    def write_config(self, settings: GlobalDriverSettings) -> tuple[bool, str]:
        """Persist to modprobe.d. Returns (ok, message). Needs elevated perms."""

    @abstractmethod
    def can_write_without_root(self) -> bool:
        """True if a polkit/helper path is configured."""

    @abstractmethod
    def persistence_status(self) -> PersistenceState:
        """Live persistence-mode state: whether the nvidia-persistenced
        service is enabled (survives reboot) + active (running now), and the
        GPU's current persistence flag from nvidia-smi. Read-only."""

    @abstractmethod
    def set_persistence(self, enabled: bool) -> tuple[bool, str]:
        """Enable/disable nvidia-persistenced (``systemctl enable/disable
        --now``). Takes effect immediately — no reboot. Returns (ok, message).
        Needs elevated perms."""


# ---------------------------------------------------------------------------
#  Per-game diagnostics (read-only probes)
# ---------------------------------------------------------------------------
class DiagnosticsPort(ABC):
    """Read-only health probes for one game's launch environment: Vulkan ICD /
    RT-extension availability, the Proton tool the game is bound to, NVAPI/DLSS
    DLLs in its prefix, and the prefix's age. Produces a verdict naming the most
    likely cause when a game reports "can't see GPU / RT blocked" — the
    diagnostic layer that distinguishes a fixable env problem from a
    Proton+driver runtime one. Never mutates state; runs off the main loop."""

    @abstractmethod
    def probe(self, game: Game) -> DiagnosticReport:
        ...


# ---------------------------------------------------------------------------
#  Per-game feature detection (read-only capability probe)
# ---------------------------------------------------------------------------
class FeatureDetectionPort(ABC):
    """Detect whether a game supports NVIDIA features (DLSS-SR / Frame Gen /
    Reflex / Ray Tracing) so the UI can gate the per-game toggles — not every
    game ships DLSS, and showing every toggle unconditionally (the old
    behaviour) misled users into enabling features a game can't use.

    Confidence is tiered and reported per-feature via
    :class:`~nvidia_gui.domain.models.GameCapability`: a user override beats an
    online community index beats the bundled curated table beats install-dir /
    prefix DLL probes, and an undecidable feature is reported as ``UNKNOWN``
    rather than silently ``True``. Read-only and offline-graceful — never
    raises; offline just yields ``UNKNOWN`` with a ``notes`` explanation."""

    @abstractmethod
    def probe(self, game: Game) -> GameCapability:
        ...


# ---------------------------------------------------------------------------
#  App settings (the app's own persisted preferences — not per-game profiles)
# ---------------------------------------------------------------------------
class SettingsPort(ABC):
    """Dotted-path key/value store for presentation-facing preferences that
    survive across launches: window geometry, pane divider positions. Kept as
    a port so the presentation layer persists them *through the use-case
    facade* (it may never import the concrete TOML adapter, per the hexagonal
    seam)."""

    @abstractmethod
    def get(self, dotted: str, default: Any = None) -> Any:
        ...

    @abstractmethod
    def set(self, dotted: str, value: Any) -> None:
        ...
