"""Domain services — pure business rules with no side effects.

These are the functions that must behave identically whether called from the
GUI, a test, or the install script. They never touch the filesystem or spawn
processes; they take domain models in and return domain models / primitives
out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath

from .models import (
    DisplayInfo,
    DlssPreset,
    DriverInfo,
    Game,
    GameProfile,
    GlobalDriverSettings,
    GpuSnapshot,
)

__all__ = [
    "build_game_env",
    "compose_registry_dwords",
    "validate_registry_dwords",
    "dlss_target_paths",
    "render_env_file",
    "recommend_optimal_profile",
    "dll_kinds",
]


# ---------------------------------------------------------------------------
#  Per-game launch environment
# ---------------------------------------------------------------------------
# The curated map: a profile flag -> the (env_var, on_value) it emits. Variable
# names are verified against the upstream dxvk-nvapi README + Proton (2026-07).
# An earlier table here emitted ghost knobs (NVAPI_LOW_LATENCY, the
# ..._DLSS_ENABLE_DLSSG name, and the SR-preset spelling missing _SELECTION)
# that dxvk-nvapi did not recognize -- the app was writing lit switches with no
# effect. A flag may share a var with another (enable_nvapi + enable_rtx both
# gate PROTON_ENABLE_NVAPI); the composer dedupes so the var is emitted once.
@dataclass(frozen=True)
class ToggleSpec:
    flag: str
    var: str
    on: str


_TOGGLES: tuple[ToggleSpec, ...] = (
    # GPU visibility -- the actual "game can't see my NVIDIA GPU" knob.
    ToggleSpec("expose_nvidia_gpu", "PROTON_HIDE_NVIDIA_GPU", "0"),
    # NVAPI (belt-and-suspenders; default-on Proton 9+, but harmless + discoverable).
    ToggleSpec("enable_nvapi",      "PROTON_ENABLE_NVAPI",    "1"),
    ToggleSpec("enable_dxvk_nvapi", "DXVK_ENABLE_NVAPI",      "1"),  # disables DXVK nvapiHack
    ToggleSpec("enable_rtx",        "PROTON_ENABLE_NVAPI",    "1"),  # RT needs NVAPI (deduped)
    # Ray tracing force via VKD3D. The real token is `dxr`; `dxr11` is a legacy
    # token modern vkd3d-proton silently ignores. (Comma-merged with extra_env.)
    ToggleSpec("force_dxr",         "VKD3D_CONFIG",           "dxr"),
    # DLSS frame generation via the DRS setting.
    ToggleSpec("enable_dlss_fg",    "DXVK_NVAPI_DRS_NGX_DLSS_FG_OVERRIDE", "on"),
    # Vulkan Reflex low-latency layer.
    ToggleSpec("enable_reflex",     "DXVK_NVAPI_VKREFLEX",    "1"),
    # Scheduling / overlay.
    ToggleSpec("enable_mangohud",   "MANGOHUD",               "1"),
    ToggleSpec("enable_gamemode",   "GAMEMODERUN",            "1"),
)

# DLSS-SR render-preset override: the dedicated DRS env var (correct spelling
# -- the previous `..._RENDER_PRESET` form, missing the trailing `_SELECTION`,
# was not a real dxvk-nvapi knob). Emitted only when a preset is chosen.
_SR_PRESET_VAR = "DXVK_NVAPI_DRS_NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION"

# VKD3D_CONFIG is a comma-separated token list; the force_dxr toggle adds `dxr`
# and a user's extra_env tokens (e.g. `descriptor_indexing`) survive alongside.
_VKD3D_CONFIG = "VKD3D_CONFIG"


def _merge_vkd3d_config(toggle_val: str, user_val: str | None) -> str:
    """Union the tokens from the force_dxr toggle and a user VKD3D_CONFIG,
    de-duplicated, order-stable (toggle token first). An empty user_val yields
    the toggle token alone; an empty toggle_val yields the user value alone."""
    tokens: list[str] = []
    if toggle_val:
        tokens.append(toggle_val)
    if user_val:
        for tok in user_val.split(","):
            tok = tok.strip()
            if tok and tok not in tokens:
                tokens.append(tok)
    return ",".join(tokens)


def build_game_env(profile: GameProfile) -> dict[str, str]:
    """Compose the deterministic env-var map for a profile.

    Precedence (highest last): curated toggles -> DLSS-SR preset -> extra_env.
    First-class toggles that share a variable are emitted once. extra_env wins
    over toggles for any collided var, EXCEPT ``VKD3D_CONFIG``, which is
    comma-merged so a user's tokens (e.g. ``descriptor_indexing``) survive next
    to the toggle's ``dxr``.
    """
    env: dict[str, str] = {}
    for spec in _TOGGLES:
        if getattr(profile, spec.flag, False):
            env[spec.var] = spec.on

    if profile.dlss_preset and profile.dlss_preset != DlssPreset.DISABLED:
        env[_SR_PRESET_VAR] = profile.dlss_preset

    # free-form last so the user can force-override anything curated. VKD3D_CONFIG
    # is merged rather than overwritten so the force_dxr `dxr` token is additive.
    for k, v in profile.extra_env.items():
        k, v = str(k), str(v)
        if k == _VKD3D_CONFIG and _VKD3D_CONFIG in env:
            merged = _merge_vkd3d_config(env[_VKD3D_CONFIG], v)
            env[_VKD3D_CONFIG] = merged or v
        else:
            env[k] = v
    return env


def render_env_file(profile: GameProfile) -> str:
    """Render the sourced shell env-file body for a profile.

    The file is sourced by the wrapper line in localconfig.vdf, so it must be
    valid POSIX sh. Values are single-quoted; stray single-quotes are escaped.
    Lines are keyed for stable diffs and easy manual editing. Trailing comment
    notes what produced it so a human editing it understands the provenance.
    """
    env = build_game_env(profile)
    lines: list[str] = [
        "# Managed by nvidia-gui. Edit freely; regenerated when you save in the app.",
        f"# appid={profile.appid}  (regenerate: open this game's profile in NVIDIA-GUI)",
        "",
    ]
    for key in sorted(env):
        val = str(env[key]).replace("'", "'\"'\"'")
        lines.append(f"export {key}='{val}'")
    if profile.dlss_dll_swap and profile.dlss_swap_version:
        # The swap itself is a real file copy (handled by the swap adapter), not
        # an env var — but we annotate the env file so people reading it know.
        lines.insert(0, f"# DLL swap: dlss={profile.dlss_swap_version}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
#  "Optimize for this rig" — derive a recommended launch env from hardware
# ---------------------------------------------------------------------------
# A pure heuristic: parse the detected GPU name for an RTX generation, then
# pick the RT / frame-gen / preset knobs current guidance recommends. Returns a
# *starting point* the user still tunes in-game (a mirror of the NVIDIA App's
# Optimize action, which "adjusts the graphics settings accordingly"). Callers
# overlay these fields onto the existing profile (preserving the DLL swap +
# extra_env), so Optimize never wipes a user's advanced choices.
_RT_WORD = re.compile(r"RTX\s*(\d{2})", re.IGNORECASE)


def _rtx_generation(gpu_name: str) -> int:
    """RTX generation number (20/30/40/50...) or 0 if not an RTX card.

    A GPU reporting "RTX" with no two-digit model number is treated as the
    oldest RTX generation (20) — conservative for FG eligibility."""
    if not gpu_name or "RTX" not in gpu_name.upper():
        return 0
    m = _RT_WORD.search(gpu_name)
    return int(m.group(1)) if m else 20


def recommend_optimal_profile(
    game: Game, gpu: GpuSnapshot, driver: DriverInfo, display: DisplayInfo
) -> GameProfile:
    """Recommend a launch-env profile for the detected hardware.

    Pure (no I/O): the use case feeds it live :class:`GpuSnapshot` /
    :class:`DriverInfo` / :class:`DisplayInfo`. Returns a fresh profile with the
    graphics/RT/DLSS/latency fields set; the caller overlays these onto the
    existing profile so a user's DLL swap + extra_env are preserved. ``driver``
    and ``display`` are accepted as a forward-compatible capability contract
    (a future RT-capable-branch check can consult ``driver.branch``).
    """
    _ = (driver, display)  # capability contract; not yet a branch, kept for callers/tests
    gen = _rtx_generation(gpu.gpu_name)
    is_rtx = gen > 0
    is_fg_capable = gen >= 40
    return GameProfile(
        appid=game.appid,
        # GPU visibility — single NVIDIA GPU: expose it (THE fix for "can't see GPU").
        expose_nvidia_gpu=True,
        enable_nvapi=True,
        enable_dxvk_nvapi=True,
        # RT + DXR only on RTX-class hardware.
        enable_rtx=is_rtx,
        force_dxr=is_rtx,
        # DLSS: newest preset recommended on 50-series, else K (balanced DLSS 4).
        dlss_preset=(DlssPreset.LETTER_N if gen >= 50 else DlssPreset.LETTER_K)
        if is_rtx else DlssPreset.DISABLED,
        enable_dlss_fg=is_fg_capable,
        # Latency + scheduling: reflex + gamemode are safe defaults; MangoHud is
        # an overlay (debug), not a graphics-quality knob — left off / preserved.
        enable_reflex=True,
        enable_gamemode=True,
        enable_mangohud=False,
    )


# ---------------------------------------------------------------------------
#  Global driver string (NVreg_RegistryDwords)
# ---------------------------------------------------------------------------
# The two RmRegistry keys we curate explicit UI toggles for. Both are
# well-established and widely cited for Linux NVreg_RegistryDwords tuning
# (PerfLevelSrc = which perf state source wins; PowerMizerEnable = whether the
# daemon-style governor runs). Values written as ``Key=Value;`` chunks.
_REGISTRY_KEYS: dict[str, re.Pattern[str]] = {
    "PerfLevelSrc": re.compile(r"^0x[0-9a-fA-F]{1,8}$"),
    "PowerMizerEnable": re.compile(r"^[01]$"),
}

# Generic shape: keys NVreg forwards to are PascalCase-ish identifiers; values
# are either a hex dword (0xNNNN), a small int, or a single 0/1 bit. We accept
# all three so power users can add un-curated keys we don't expose in the UI.
_KEY_SHAPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_VAL_SHAPE = re.compile(r"^(0x[0-9a-fA-F]{1,8}|[0-9]{1,4}|[01])$")


def validate_registry_dwords(raw: str) -> list[str]:
    """Return a list of human-readable problems in *raw*, or [] if clean.

    Never raises: a malformed input just yields problems so the UI can show
    them. A clean string is safe to write into modprobe.d. Curated keys are
    strictly validated; un-curated keys are shape-checked loosely so the GUI
    isn't a false gate against real RmRegistry string the user knows about.
    """
    problems: list[str] = []
    if not raw or not raw.strip():
        return []  # empty is valid (clears the registry line)
    # NVreg tolerates ';' separators. We accept ',' too and normalise on write.
    for chunk in raw.replace(",", ";").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            problems.append(f"'{chunk}' is not 'Key=Value'")
            continue
        key, _, val = chunk.partition("=")
        key, val = key.strip(), val.strip()
        if not _KEY_SHAPE.fullmatch(key):
            problems.append(f"'{key}' is not a valid registry key name")
            continue
        pat = _REGISTRY_KEYS.get(key, _VAL_SHAPE)
        if not pat.fullmatch(val):
            if key in _REGISTRY_KEYS:
                problems.append(f"'{key}={val}' fails curated value validation")
            else:
                problems.append(f"'{key}={val}': value must be hex/int/0-1")
    return problems


def compose_registry_dwords(
    rebar: bool, tweak_pairs: list[tuple[str, str]]
) -> str:
    """Compose a normalised NVreg_RegistryDwords string.

    ``tweak_pairs`` is the user's chosen (Key, Value) list; it is *validated*
    here — anything invalid is skipped (callers should pre-check with
    :func:`validate_registry_dwords` to surface problems to the user). ReBAR
    is applied as its own ``NVreg_EnableResizableBar=1`` line by the kernel
    adapter, not in this string; the ``rebar`` param is accepted for symmetry.

    >>> compose_registry_dwords(False, [("PerfLevelSrc", "0x3322"),
    ...                                  ("PowerMizerEnable", "1"),
    ...                                  ("Bogus", "nope"),
    ...                                  ("PerfLevelSrc", "bad")])
    'PerfLevelSrc=0x3322;PowerMizerEnable=1;'
    """
    pairs: list[str] = []
    for key, val in tweak_pairs:
        key, val = key.strip(), val.strip()
        pat = _REGISTRY_KEYS.get(key, _VAL_SHAPE)
        if _KEY_SHAPE.fullmatch(key) and pat.fullmatch(val):
            pairs.append(f"{key}={val}")
    # trailing ';' keeps the string unambiguous; NVreg tolerates it.
    return ";".join(pairs) + (";" if pairs else "")


# ---------------------------------------------------------------------------
#  DLSS swap target resolution
# ---------------------------------------------------------------------------
# Filenames the swapper copies. ``dlss`` is the only required one; the rest
# are copied if present in the source bundle.
_DLL_NAMES = {
    "dlss": "nvngx_dlss.dll",
    "dlssd": "nvngx_dlssd.dll",
    "dlssg": "nvngx_dlssg.dll",
    "low_latency_vk": "NvLowLatencyVk.dll",
    "deepdvc": "nvngx_deepdvc.dll",
}


def dll_filename(kind: str) -> str:
    """Resolve a bundle 'kind' to the DLL filename on disk."""
    return _DLL_NAMES[kind]


def dll_kinds() -> list[str]:
    """All known DLSS DLL kinds, in canonical order — for a full sweep
    (swap status / revert) when no single bundle is in hand."""
    return list(_DLL_NAMES)


def dlss_target_paths(
    installdir_abs: str,
    prefix_system32_abs: str | None,
    kinds: list[str],
) -> list[tuple[str, str]]:
    """Compute (game_dir, prefix) target candidates for each requested DLL kind.

    Strategy: the game's own dir is highest-precedence in Proton's DLL search,
    then ``drive_c/windows/system32`` in the compat prefix. We try *both* so a
    swap lands wherever the game will actually find it. Returns absolute paths.
    """
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()

    def consider(p: str) -> None:
        if p and p not in seen:
            seen.add(p)
            targets.append((k, p))

    for k in kinds:
        fname = _DLL_NAMES.get(k)
        if not fname:
            continue
        # game install root first
        consider(str(PurePath(installdir_abs) / fname))
        # common subdirs games put DLLs in
        for sub in ("Binaries/Win64", "bin", "Win64", "Binaries/Win32"):
            consider(str(PurePath(installdir_abs) / sub / fname))
        # prefix system32 last
        if prefix_system32_abs:
            consider(str(PurePath(prefix_system32_abs) / fname))
    return targets


def summarise_toggles(profile: GameProfile) -> list[str]:
    """Short human-readable list of what's ON in a profile — for the games list.

    Reports the real emitted behaviour (no ghost knobs); ``extra_env`` entries
    are surfaced so a profile that relies on custom vars isn't shown empty."""
    flags: list[str] = []
    if not profile.expose_nvidia_gpu:
        flags.append("GPU hidden")           # unusual — surface it
    if profile.enable_rtx:
        flags.append("RT")
    if profile.force_dxr:
        flags.append("DXR")
    if profile.dlss_preset and profile.dlss_preset != DlssPreset.DISABLED:
        flags.append(f"DLSS {profile.dlss_preset}")
    if profile.enable_dlss_fg:
        flags.append("FG")
    if profile.enable_reflex:
        flags.append("Reflex")
    if not profile.enable_nvapi:
        flags.append("NVAPI off")
    if not profile.enable_dxvk_nvapi:
        flags.append("DXVK-NVAPI off")
    if profile.enable_gamemode:
        flags.append("gamemode")
    if profile.enable_mangohud:
        flags.append("MangoHud")
    if profile.dlss_dll_swap:
        flags.append(f"DLL {profile.dlss_swap_version or '?'}")
    if profile.extra_env:
        flags.append(f"+{len(profile.extra_env)} env")
    return flags


# small re-export for the GlobalDriverSettings default display
def settings_summary(s: GlobalDriverSettings) -> str:
    parts: list[str] = []
    parts.append("ReBAR " + ("on" if s.enable_resizable_bar else "off"))
    if s.registry_dwords:
        parts.append("registry: " + s.registry_dwords)
    return ", ".join(parts) or "stock"
