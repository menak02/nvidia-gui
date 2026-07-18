"""Domain models — value objects and small aggregates.

Deliberately dependency-free. The types describe *what* the application reasons
about, not *where* it is stored or *how* it is applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Launcher",
    "Game",
    "GameProfile",
    "DlssPreset",
    "DlssVersion",
    "DlssBundle",
    "GlobalDriverSettings",
    "GpuSnapshot",
    "DriverInfo",
    "DisplayInfo",
    "DiagStatus",
    "DiagnosticCheck",
    "DiagnosticReport",
    "PersistenceState",
    "FeatureSource",
    "FeatureFlag",
    "GameCapability",
]


# ---------------------------------------------------------------------------
#  Enums-as-constants (kept as plain str to stay stdlib-serialisable in TOML)
# ---------------------------------------------------------------------------
class Launcher:
    """Where a game was detected. Kept as constants, not an Enum, so they
    round-trip through TOML as plain strings without custom serializers."""

    STEAM = "steam"
    LUTRIS = "lutris"
    UNKNOWN = "unknown"


class DlssPreset:
    """NVIDIA DLSS-SR render presets, exposed as Proton NVAPI override values.

    The _LETTER flavors (e.g. ``j``, ``k``) are the modern DLSS 4 preset codes;
    the numeric flavors are the legacy DLSS 2/3 codes. Both are real strings
    that Proton's NVAPI layer understands when written to
    ``NVAPI_DLSS_SR_PRESET_OVERRIDE``.
    """

    LETTER_A = "a"
    LETTER_B = "b"
    LETTER_C = "c"
    LETTER_D = "d"
    LETTER_E = "e"
    LETTER_F = "f"
    LETTER_G = "g"
    LETTER_I = "i"
    LETTER_J = "j"
    LETTER_K = "k"
    LETTER_L = "l"
    LETTER_M = "m"
    LETTER_N = "n"
    LETTER_O = "o"
    LETTER_P = "p"

    DISABLED = "off"

    # All DLSS letter presets (A through P).
    CHOICES: tuple[tuple[str, str], ...] = (
        (DISABLED, "Disabled (use game default)"),
        (LETTER_A, "Preset A"),
        (LETTER_B, "Preset B"),
        (LETTER_C, "Preset C"),
        (LETTER_D, "Preset D"),
        (LETTER_E, "Preset E"),
        (LETTER_F, "Preset F"),
        (LETTER_G, "Preset G"),
        (LETTER_I, "Preset I"),
        (LETTER_J, "Preset J"),
        (LETTER_K, "Preset K (recommended, DLSS 4)"),
        (LETTER_L, "Preset L"),
        (LETTER_M, "Preset M"),
        (LETTER_N, "Preset N (DLSS 4 — latest)"),
        (LETTER_O, "Preset O"),
        (LETTER_P, "Preset P"),
    )


# ---------------------------------------------------------------------------
#  Game + profile
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Game:
    """A detected installable, identified by launcher + that launcher's id.

    ``appid`` is a string so it round-trips through (VDF/TOML/JSON) losslessly.
    ``installdir`` is the relative folder under ``steamapps/common`` (Steam) or
    the absolute prefix path (Lutris); the adapters resolve it to an absolute
    path via their library root.
    """

    appid: str
    name: str
    installdir: str
    launcher: str = Launcher.STEAM

    def __post_init__(self) -> None:
        if not self.appid:
            raise ValueError("Game requires a non-empty appid")
        if not self.name:
            raise ValueError(f"Game {self.appid!r} requires a name")


@dataclass
class GameProfile:
    """Per-game tuning, written out as both a TOML profile and a sourced
    shell env-file that localconfig.vdf references.

    Field → emitted variable (verified against the dxvk-nvapi upstream README +
    Proton, 2026-07-10). Composition precedence lives in :mod:`services`:
    toggles first, then :attr:`dlss_preset`, then :attr:`extra_env` wins.

    ``extra_env`` is a free-form key/value map for anything the curated toggles
    don't cover (e.g. ``WINEESYNC=1``). It is *not* a dump of the curated keys —
    those are emitted deterministically by :mod:`services`.
    """

    appid: str
    # GPU & ray tracing
    expose_nvidia_gpu: bool = True    # -> PROTON_HIDE_NVIDIA_GPU=0  (the GPU-visibility fix)
    enable_nvapi: bool = True         # -> PROTON_ENABLE_NVAPI=1    (belt-and-suspenders; default-on Proton 9+)
    enable_dxvk_nvapi: bool = True    # -> DXVK_ENABLE_NVAPI=1      (disables DXVK nvapiHack)
    enable_rtx: bool = False          # RT gate -> PROTON_ENABLE_NVAPI=1  (deduped with enable_nvapi)
    force_dxr: bool = False           # -> VKD3D_CONFIG=dxr        (real force token; dxr11 is ignored on modern vkd3d-proton)
    # DLSS
    dlss_preset: str = DlssPreset.DISABLED
    enable_dlss_fg: bool = False      # -> DXVK_NVAPI_DRS_NGX_DLSS_FG_OVERRIDE=on  (frame generation)
    dlss_fg_preset: str = "default"   # -> DXVK_NVAPI_DRS_NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION
    dlss_fg_mode: str = "default"     # -> DXVK_NVAPI_DRS_NGX_DLSSG_MODE
    dlss_fg_multiplier: int = 0       # -> DXVK_NVAPI_DRS_NGX_DLSSG_MULTI_FRAME_COUNT
    # Latency & scheduling
    enable_reflex: bool = False        # -> DXVK_NVAPI_VKREFLEX=1   (Vulkan Reflex layer)
    enable_gamemode: bool = False      # -> GAMEMODERUN=1
    enable_mangohud: bool = False      # -> MANGOHUD=1
    # DLSS DLL swap (opt-in physical file copy of a Streamline SDK DLL)
    dlss_dll_swap: bool = False
    dlss_swap_version: str = ""        # e.g. "v2.12.0"; "" => no swap
    # free-form escape hatch — always wins over the toggles above (applied last)
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass
class GlobalDriverSettings:
    """System-wide driver tuning written to ``/etc/modprobe.d/nvidia-gui.conf``
    and applied at next boot.

    ``registry_dwords`` is the exact ``NVreg_RegistryDwords`` string (e.g.
    ``"PerfLevelSrc=0x3322;"``). It is composed/validated in :mod:`services`,
    never trusted raw from the UI.
    """

    enable_resizable_bar: bool = False
    registry_dwords: str = ""
    registry_dwords_per_device: str = ""
    notes: str = ""


@dataclass
class PersistenceState:
    """Live state of NVIDIA persistence mode — the GPU staying "loaded"
    between accesses so it doesn't re-initialise on a focus change (the
    tab-in stutter). Reported from the system ``nvidia-persistenced``
    service plus ``nvidia-smi``.

    ``nvidia-smi -pm 1`` sets the live flag but drops on module reload/reboot;
    enabling the ``nvidia-persistenced`` service makes it survive boot. That
    survival is exactly what the modprobe.d ReBAR/registry switch *cannot*
    give you — those knobs are load-time, persistence is a runtime daemon.
    Intentionally a separate concern from :class:`GlobalDriverSettings`: the
    modprobe port owns boot-time kernel options, persistence owns a running
    systemd service.
    """

    survives_reboot: bool = False   # systemctl is-enabled nvidia-persistenced == enabled
    active: bool = False            # systemctl is-active nvidia-persistenced == active
    live_mode: bool = False         # nvidia-smi persistence_mode == Enabled
    supported: bool = True           # nvidia-persistenced binary present at all


# ---------------------------------------------------------------------------
#  DLSS cache
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DlssVersion:
    """One installed DLSS package in the managed cache, e.g. ``v2.12.0``."""

    version: str
    path: str  # the directory holding nvngx_dlss.dll and friends

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("DlssVersion requires a version")


@dataclass(frozen=True)
class DlssBundle:
    """The set of Streamline DLLs that belong to one cached version.

    Only ``dlss`` (nvngx_dlss.dll) is strictly required; the others are present
    when the seed/archive supplied them."""

    version: str
    dlss: str = ""        # nvngx_dlss.dll   (frame reconstruction)
    dlssd: str = ""       # nvngx_dlssd.dll  (debug variant)
    dlssg: str = ""       # nvngx_dlssg.dll  (frame generation)
    low_latency_vk: str = ""   # NvLowLatencyVk.dll
    deepdvc: str = ""     # nvngx_deepdvc.dll (DLAA / deep a-svc)

    def available_dlls(self) -> list[tuple[str, str]]:
        """Return only the (kind, filename) pairs that are present (non-empty).

        Returned as a fresh list of tuples (not a dict_items view) so callers can
        iterate it freely and the bundle stays immutable.
        """
        pairs = (
            ("dlss", self.dlss),
            ("dlssd", self.dlssd),
            ("dlssg", self.dlssg),
            ("low_latency_vk", self.low_latency_vk),
            ("deepdvc", self.deepdvc),
        )
        return [(kind, name) for kind, name in pairs if name]


# ---------------------------------------------------------------------------
#  Telemetry / status snapshots (read-only value objects)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GpuSnapshot:
    """One poll of nvidia-smi. Missing readings default to ``""`` so the UI
    can render '—' rather than crashing on an absent field."""

    gpu_name: str = ""
    driver_version: str = ""
    temperature_c: str = ""
    gpu_util_pct: str = ""
    mem_util_pct: str = ""
    power_draw_w: str = ""
    power_limit_w: str = ""
    gr_clock_mhz: str = ""
    mem_clock_mhz: str = ""
    mem_used_mb: str = ""
    mem_total_mb: str = ""
    fan_pct: str = ""


@dataclass(frozen=True)
class DriverInfo:
    """Static-ish driver/module facts, gathered once per session."""

    driver_version: str = ""
    module_version: str = ""
    branch: str = ""          # e.g. "open" or "proprietary" (best effort)
    module_name: str = "nvidia"
    modprobe_config_path: str = ""
    compositor_incompatible: bool = False  # nvidia-settings writes inert?

    def display(self) -> str:
        return f"{self.driver_version} ({self.branch})"


@dataclass(frozen=True)
class DisplayInfo:
    """Wayland-centered, read-only. On Linux we cannot *set* most display
    state the way the Windows App can (mode switching, vibrance on a fullscreen
    exclusive surface), so this is informational."""

    server: str = ""            # "Wayland" / "X11" / "Unknown"
    monitors: tuple[str, ...] = ()
    vrr_capable: bool = False
    gsync_capable: bool = False
    notes: str = ""


# ---------------------------------------------------------------------------
#  Per-game diagnostics (read-only value objects)
# ---------------------------------------------------------------------------
class DiagStatus:
    """Status constants for a diagnostic check. Plain strings (not an Enum) so
    the reports read cleanly in logs/TOML without custom serialisers."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    INFO = "info"


@dataclass(frozen=True)
class DiagnosticCheck:
    """One probe result. ``fix_hint`` is a short suggested remediation shown in
    the UI (the app performs no destructive action — rebuild/swap hints are
    documentation, not buttons)."""

    name: str
    status: str = DiagStatus.INFO
    detail: str = ""
    fix_hint: str = ""


@dataclass(frozen=True)
class DiagnosticReport:
    """A set of per-game, read-only probes plus a one-line most-likely-cause
    verdict synthesised from them. Surface every check (even OK ones) so the
    card shows *what was tested*, not just what failed."""

    appid: str
    checks: tuple[DiagnosticCheck, ...] = ()
    verdict: str = ""


# ---------------------------------------------------------------------------
#  Per-game DETECTED feature support (capability — distinct from GameProfile,
#  which is the user's *applied* toggles)
# ---------------------------------------------------------------------------
class FeatureSource:
    """Where a per-game capability conclusion came from. Plain strings (not an
    Enum) so they serialise through TOML/JSON/logs cleanly. The detection
    adapter merges per-feature with this precedence, highest-confidence first;

    an undecided feature reads as :attr:`UNKNOWN` — the detector never silently
    reports ``True`` (that was the old unconditional-toggle bug)."""

    OVERRIDE = "user"             # explicit user override (highest)
    ONLINE = "community-db"       # fetched from a community DLSS-games index
    BUNDLED = "curated"           # the shipped assets/nvidia_features.toml floor
    INSTALLDIR = "dll-install"    # nvngx_dlss.dll found in the game install dir
    PREFIX = "dll-prefix"         # nvapi/dlss DLLs in the Proton prefix system32
    UNKNOWN = "unknown"           # no source could decide (lowest)


@dataclass(frozen=True)
class FeatureFlag:
    """One detected feature: whether it's supported, and how we know.

    Carrying ``source`` lets the UI render 'detected vs curated vs overridden'
    honestly — a bare bool would conflate 'actively probed a DLL' with 'looked
    the appid up in a table', and the whole point of this batch is that the
    distinction matters to the user ('not all games come with them')."""

    supported: bool
    source: str = FeatureSource.UNKNOWN


@dataclass(frozen=True)
class GameCapability:
    """Detected NVIDIA-feature support for one game — the *capability* side,
    read-only, distinct from :class:`GameProfile` (the user's applied toggles).

    Built by the :class:`~nvidia_gui.application.ports.FeatureDetectionPort`
    adapter; pure data, no I/O. ``notes`` carries a one-line provenance/warning
    (e.g. 'offline — install-dir probe only') that the UI shows under the chip.
    ``dlss_version`` is the parsed FileVersion from nvngx_dlss.dll (e.g. "4.1.0.0")
    when available; None means version unknown (skip version-based gating).
    """

    appid: str
    dlss_sr: FeatureFlag = field(default_factory=lambda: FeatureFlag(False))
    dlss_fg: FeatureFlag = field(default_factory=lambda: FeatureFlag(False))
    reflex: FeatureFlag = field(default_factory=lambda: FeatureFlag(False))
    rt: FeatureFlag = field(default_factory=lambda: FeatureFlag(False))
    notes: str = ""
    dlss_version: str | None = None

    def is_known(self) -> bool:
        """True iff at least one feature resolved above the UNKNOWN tier — i.e.
        the pipeline produced a real answer rather than the fallback."""
        return any(f.source != FeatureSource.UNKNOWN
                   for f in (self.dlss_sr, self.dlss_fg, self.reflex, self.rt))
