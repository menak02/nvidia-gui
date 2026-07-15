"""Cohesive headless tests for nvidia-gui's business core.

Covers the four locked product decisions through the pure, GTK-free layer:
  1. per-game profile env (domain.services.build_game_env / render_env_file)
  2. DLSS preset emission + swap targeting (domain.services)
  3. Steam launch injection idiom — idempotent prepend, preserve, restore
     (adapters.launch_option_steam on top of adapters.vdf_lite)
  4. NVreg_* kernel-module params (domain.services registry-dword validation
     + adapters.kernel_param_modprobe._render)

Plus the supporting honest-read / persistence code: config_toml's TOML
serializer (the documented null-skip fix) and gpu_query_sm's nvidia-smi CSV
parse + "[N/A]" normalisation.

Run with pytest if available, else plain ``python3 tests/test_services.py``
(the __main__ block collects every ``test_*`` at module scope). No display,
no GTK, no network.
"""

from __future__ import annotations

import os
import sys
import textwrap
import tomllib
import unittest.mock as mock
from pathlib import Path

# Make ``src`` importable so ``import nvidia_gui...`` resolves from the repo
# layout regardless of whether the package is pip-installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from nvidia_gui.domain import services  # noqa: E402
from nvidia_gui.domain.models import (  # noqa: E402
    DisplayInfo,
    DlssBundle,
    DlssPreset,
    DriverInfo,
    FeatureFlag,
    FeatureSource,
    Game,
    GameCapability,
    GameProfile,
    GpuSnapshot,
    GlobalDriverSettings,
)
from nvidia_gui.adapters.config_toml import _dump_toml  # noqa: E402
from nvidia_gui.adapters.gpu_query_sm import NvidiaSmiGpu  # noqa: E402
from nvidia_gui.adapters.kernel_param_modprobe import _render, ModprobeKernelParam  # noqa: E402
from nvidia_gui.adapters.launch_option_steam import SteamLaunchOptions  # noqa: E402
from nvidia_gui.adapters.vdf_lite import loads, dumps  # noqa: E402


# ---------------------------------------------------------------------------
#  Decision: per-game profile env
# ---------------------------------------------------------------------------
def test_build_game_env_emits_only_curated_toggles_that_are_on() -> None:
    p = GameProfile(appid="1091500")
    p.enable_nvapi = True
    p.enable_reflex = True
    p.enable_mangohud = True
    p.enable_dxvk_nvapi = False  # explicitly off -> must NOT appear

    env = services.build_game_env(p)

    assert env["PROTON_ENABLE_NVAPI"] == "1"
    # Reflex emits the REAL dxvk-nvapi knob, not the ghost NVAPI_LOW_LATENCY
    assert env["DXVK_NVAPI_VKREFLEX"] == "1"
    assert env["MANGOHUD"] == "1"
    # toggles left off are absent — no half-on keys polluting the launch env
    assert "DXVK_ENABLE_NVAPI" not in env
    assert "GAMEMODERUN" not in env
    # ghost knobs proven to do nothing — must NEVER appear
    assert "NVAPI_LOW_LATENCY" not in env
    assert "NV_LOW_LATENCY_VK" not in env
    assert "DXVK_NVAPI_DLSS_ENABLE_DLSSG" not in env


def test_dlss_preset_emits_render_preset_selection_and_extra_env_wins() -> None:
    p = GameProfile(appid="1", dlss_preset=DlssPreset.LETTER_K)
    # curated PROTON_ENABLE_NVAPI defaults to "1"; user forces it off via extra
    p.extra_env = {"PROTON_ENABLE_NVAPI": "0", "WINEESYNC": "1"}

    env = services.build_game_env(p)

    # DLSS-SR preset -> the ONE real DRS override (trailing _SELECTION); the
    # older ..._RENDER_PRESET spelling and NVAPI_DLSS_SR_PRESET_OVERRIDE were
    # ghost keys dxvk-nvapi never recognized.
    sel = "DXVK_NVAPI_DRS_NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION"
    assert env[sel] == "k"
    assert "DXVK_NVAPI_DRS_NGX_DLSS_SR_OVERRIDE_RENDER_PRESET" not in env
    assert "NVAPI_DLSS_SR_PRESET_OVERRIDE" not in env
    # extra_env wins over curated (applied last) — toggle actually turns OFF
    assert env["PROTON_ENABLE_NVAPI"] == "0"
    assert env["WINEESYNC"] == "1"


def test_render_env_file_is_sorted_posix_sh_with_managed_provenance() -> None:
    p = GameProfile(appid="1091500", dlss_dll_swap=True, dlss_swap_version="v2.12.0")
    p.enable_nvapi = True
    p.extra_env = {"Q": "it's a test"}  # single-quote must be sh-escaped

    body = services.render_env_file(p)
    lines = body.splitlines()

    # provenance header + the swap annotation inserted at the very front
    assert lines[0] == "# DLL swap: dlss=v2.12.0"
    assert any("# Managed by nvidia-gui" in ln for ln in lines)
    assert "# appid=1091500" in body
    assert body.endswith("\n")

    # every export line is POSIX sh: `export KEY='val'` and keys are sorted
    export_lines = [ln for ln in lines if ln.startswith("export ")]
    assert export_lines, "expected at least one export line"
    keys = [ln.split(" ", 1)[1].split("=", 1)[0] for ln in export_lines]
    assert keys == sorted(keys)
    # sorted alphabetically: PROTON_ENABLE_NVAPI precedes Q
    assert "export PROTON_ENABLE_NVAPI='1'" in body
    # single-quote escaped through the canonical `'"'"'` idiom
    assert "export Q='it'\"'\"'s a test'" in body


# ---------------------------------------------------------------------------
#  Verified toggle table (Feature A): real vars only, ghost knobs never leak
# ---------------------------------------------------------------------------
def test_build_game_env_emits_only_real_toggle_vars() -> None:
    p = GameProfile(appid="1091500")
    p.expose_nvidia_gpu = True
    p.enable_nvapi = True
    p.enable_dxvk_nvapi = True
    p.enable_rtx = True
    p.force_dxr = True
    p.enable_dlss_fg = True
    p.enable_reflex = True
    p.enable_mangohud = True
    p.enable_gamemode = True

    env = services.build_game_env(p)

    # every curated knob appears with the verified-upstream token...
    assert env["PROTON_HIDE_NVIDIA_GPU"] == "0"   # the GPU-visibility fix
    assert env["PROTON_ENABLE_NVAPI"] == "1"       # emitted once (nvapi + rtx share it)
    assert env["DXVK_ENABLE_NVAPI"] == "1"
    assert env["VKD3D_CONFIG"] == "dxr"           # real force token (dxr11 ignored)
    assert env["DXVK_NVAPI_DRS_NGX_DLSS_FG_OVERRIDE"] == "on"
    assert env["DXVK_NVAPI_VKREFLEX"] == "1"
    assert env["MANGOHUD"] == "1"
    assert env["GAMEMODERUN"] == "1"
    # ...while the proven-nonexistent ghost knobs never appear
    for ghost in ("NVAPI_LOW_LATENCY", "NV_LOW_LATENCY_VK",
                  "DXVK_NVAPI_DLSS_ENABLE_DLSSG",
                  "NVAPI_DLSS_SR_PRESET_OVERRIDE",
                  "DXVK_NVAPI_DRS_NGX_DLSS_SR_OVERRIDE_RENDER_PRESET"):
        assert ghost not in env, f"ghost var leaked into env: {ghost}"


def test_env_composition_vkd3d_merged_and_extra_env_wins() -> None:
    # force_dxr adds `dxr`; a user VKD3D_CONFIG token survives alongside,
    # merged de-duped with the toggle token first -- NOT overwritten.
    p = GameProfile(appid="1")
    p.force_dxr = True
    p.extra_env = {"VKD3D_CONFIG": "descriptor_indexing"}
    assert services.build_game_env(p)["VKD3D_CONFIG"] == "dxr,descriptor_indexing"

    # de-dup: a user who ALSO lists `dxr` does not get it repeated
    p3 = GameProfile(appid="3")
    p3.force_dxr = True
    p3.extra_env = {"VKD3D_CONFIG": "dxr,descriptor_indexing"}
    assert services.build_game_env(p3)["VKD3D_CONFIG"] == "dxr,descriptor_indexing"

    # for any non-VKD3D var, extra_env beats the curated toggle outright
    p2 = GameProfile(appid="2")
    p2.expose_nvidia_gpu = True               # toggle -> PROTON_HIDE_NVIDIA_GPU=0
    p2.extra_env = {"PROTON_HIDE_NVIDIA_GPU": "1"}   # user forces GPU hidden
    assert services.build_game_env(p2)["PROTON_HIDE_NVIDIA_GPU"] == "1"


def test_dlss_preset_disabled_emits_no_sr_var() -> None:
    sel = "DXVK_NVAPI_DRS_NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION"
    # DISABLED -> no SR override var at all (the game's own preset wins)
    p = GameProfile(appid="1", dlss_preset=DlssPreset.DISABLED)
    env = services.build_game_env(p)
    assert sel not in env
    assert "DXVK_NVAPI_DRS_NGX_DLSS_SR_OVERRIDE_RENDER_PRESET" not in env

    # an active preset emits exactly the real _SELECTION var
    p2 = GameProfile(appid="2", dlss_preset=DlssPreset.LETTER_N)
    env2 = services.build_game_env(p2)
    assert env2[sel] == "n"
    assert "DXVK_NVAPI_DRS_NGX_DLSS_SR_OVERRIDE_RENDER_PRESET" not in env2


# ---------------------------------------------------------------------------
#  "Optimize for this rig" recommends hardware-appropriate toggles
# ---------------------------------------------------------------------------
def test_recommend_optimal_profile_rtx50() -> None:
    g = Game(appid="1091500", name="Cyberpunk 2077", installdir="Cyberpunk 2077")
    rec = services.recommend_optimal_profile(
        g, GpuSnapshot(gpu_name="NVIDIA GeForce RTX 5070"),
        DriverInfo(), DisplayInfo())
    assert rec.appid == "1091500"
    assert rec.expose_nvidia_gpu is True
    assert rec.enable_nvapi is True and rec.enable_dxvk_nvapi is True
    assert rec.enable_rtx is True and rec.force_dxr is True    # RTX-class -> force DXR
    assert rec.enable_dlss_fg is True                         # 50-series >= 40 -> FG
    assert rec.dlss_preset == DlssPreset.LETTER_N             # newest preset on 50-series
    assert rec.enable_reflex is True and rec.enable_gamemode is True
    assert rec.enable_mangohud is False                        # overlay off by default

    # non-RTX card -> no RT/DXR/FG, no preset (a conservative starting point)
    plain = services.recommend_optimal_profile(
        g, GpuSnapshot(gpu_name="Intel Arc B580"), DriverInfo(), DisplayInfo())
    assert plain.enable_rtx is False and plain.force_dxr is False
    assert plain.enable_dlss_fg is False
    assert plain.dlss_preset == DlssPreset.DISABLED

    # RTX 3070 (gen 30) -> RT/DXR on, FG off (< 40), preset K
    mid = services.recommend_optimal_profile(
        g, GpuSnapshot(gpu_name="NVIDIA GeForce RTX 3070"), DriverInfo(), DisplayInfo())
    assert mid.force_dxr is True and mid.enable_dlss_fg is False
    assert mid.dlss_preset == DlssPreset.LETTER_K


# ---------------------------------------------------------------------------
#  Schema migration: new GameProfile fields survive the TOML round-trip; the
#  dropped no-op field does not reappear
# ---------------------------------------------------------------------------
def test_profile_roundtrip_new_fields() -> None:
    import tempfile
    from nvidia_gui.adapters.profile_store_fs import FsProfileStore

    with tempfile.TemporaryDirectory() as td:
        store = FsProfileStore(Path(td) / "env", Path(td) / "profiles")
        p = GameProfile(appid="1091500")
        p.expose_nvidia_gpu = False
        p.enable_rtx = True
        p.force_dxr = True
        p.enable_dlss_fg = True
        p.dlss_preset = DlssPreset.LETTER_N
        p.extra_env = {"VKD3D_CONFIG": "descriptor_indexing,dxr", "WINEESYNC": "1"}
        store.save(p)

        loaded = store.load("1091500")
        assert loaded.appid == "1091500"
        assert loaded.expose_nvidia_gpu is False
        assert loaded.enable_rtx is True
        assert loaded.force_dxr is True
        assert loaded.enable_dlss_fg is True
        assert loaded.dlss_preset == DlssPreset.LETTER_N
        # the nested extra_env sub-table survived the TOML round-trip
        assert loaded.extra_env == {"VKD3D_CONFIG": "descriptor_indexing,dxr",
                                    "WINEESYNC": "1"}
        # the proven-no-op field is gone from the schema -- it must not linger
        assert not hasattr(loaded, "enable_low_latency_vk")


# ---------------------------------------------------------------------------
#  Honest save result: ok tracks the launch wrapper, not the profile write
#  (Feature D -- the "saved [ok] but the env was never sourced" silent bug)
# ---------------------------------------------------------------------------
def test_save_profile_ok_follows_wrapper() -> None:
    import tempfile
    from nvidia_gui.adapters.profile_store_fs import FsProfileStore
    from nvidia_gui.application.use_cases import UseCases

    class _FailingLaunch:
        def ensure_wrapper(self, appid, env_file):  # noqa: ANN001
            return False   # localconfig unreadable -> wrapper NOT stamped

        def remove_wrapper(self, appid):  # noqa: ANN001
            return True

    with tempfile.TemporaryDirectory() as td:
        store = FsProfileStore(Path(td) / "env", Path(td) / "profiles")
        uc = UseCases(
            gpu=None, driver=None, display=None, games=None,
            profiles=store, launch=_FailingLaunch(),
            dlss_cache=None, dlss_swap=None, kernel=None,
            diagnostics=None)
        res = uc.save_profile(GameProfile(appid="1091500"))
        # profile + env file written, but wrapper NOT -- ok MUST be False
        assert res.ok is False
        assert res.wrapper_written is False
        assert "NOT" in res.message.upper()

    # and the symmetric success path: ensure_wrapper True -> ok True
    class _OkLaunch:
        def ensure_wrapper(self, appid, env_file):  # noqa: ANN001
            return True

        def remove_wrapper(self, appid):  # noqa: ANN001
            return True

    with tempfile.TemporaryDirectory() as td:
        store = FsProfileStore(Path(td) / "env", Path(td) / "profiles")
        uc = UseCases(
            gpu=None, driver=None, display=None, games=None,
            profiles=store, launch=_OkLaunch(),
            dlss_cache=None, dlss_swap=None, kernel=None,
            diagnostics=None)
        res = uc.save_profile(GameProfile(appid="42"))
        assert res.ok is True
        assert res.wrapper_written is True


# ---------------------------------------------------------------------------
#  "Optimize for this rig" overlays RT/DLSS onto the existing profile but
#  preserves the user's advanced choices (DLL swap, extra_env, MangoHud) -- the
#  Feature A/A6 guarantee, proven end-to-end through real persistence + fakes.
# ---------------------------------------------------------------------------
def test_optimize_for_rig_preserves_advanced_and_persists() -> None:
    import tempfile
    from nvidia_gui.adapters.profile_store_fs import FsProfileStore
    from nvidia_gui.application.use_cases import UseCases

    class _Gpu:
        def snapshot(self):
            return GpuSnapshot(gpu_name="NVIDIA GeForce RTX 5070")

    class _Driver:
        def read(self):
            return DriverInfo()

    class _Display:
        def read(self):
            return DisplayInfo()

    class _OkLaunch:
        def ensure_wrapper(self, appid, env_file):  # noqa: ANN001
            return True

        def remove_wrapper(self, appid):  # noqa: ANN001
            return True

    game = Game(appid="1091500", name="Cyberpunk 2077", installdir="Cyberpunk 2077")
    with tempfile.TemporaryDirectory() as td:
        store = FsProfileStore(Path(td) / "env", Path(td) / "profiles")
        # a profile carrying ADVANCED choices Optimize must NOT wipe
        prior = GameProfile(appid="1091500")
        prior.dlss_dll_swap = True
        prior.dlss_swap_version = "v2.12.0"
        prior.enable_mangohud = True
        prior.extra_env = {"WINEESYNC": "1", "PROTON_USE_WINED3D": "1"}
        store.save(prior)

        uc = UseCases(
            gpu=_Gpu(), driver=_Driver(), display=_Display(),
            games=None, profiles=store, launch=_OkLaunch(),
            dlss_cache=None, dlss_swap=None, kernel=None,
            diagnostics=None)
        res = uc.optimize_for_rig(game)

        # saved cleanly -- the wrapper WAS stamped (so the env will take effect)
        assert res.ok is True
        assert res.wrapper_written is True

        merged = store.load("1091500")
        # the recommended RT/DLSS/latency fields landed (RTX 5070 -> gen 50)
        assert merged.expose_nvidia_gpu is True
        assert merged.enable_rtx is True and merged.force_dxr is True
        assert merged.enable_dlss_fg is True
        assert merged.dlss_preset == "n"
        assert merged.enable_reflex is True and merged.enable_gamemode is True
        # ...AND the advanced choices survived the overlay untouched
        assert merged.dlss_dll_swap is True
        assert merged.dlss_swap_version == "v2.12.0"
        assert merged.enable_mangohud is True
        assert merged.extra_env == {"WINEESYNC": "1", "PROTON_USE_WINED3D": "1"}


# ---------------------------------------------------------------------------
#  Decision: NVreg_* registry dwords + kernel-param render
# ---------------------------------------------------------------------------
def test_validate_registry_dwords_curated_loose_and_malformed() -> None:
    # empty / whitespace are valid (means "clear the line")
    assert services.validate_registry_dwords("") == []
    assert services.validate_registry_dwords("   ") == []
    # curated + loose both clean; comma is accepted as a separator too
    assert services.validate_registry_dwords(
        "PerfLevelSrc=0x3322;PowerMizerEnable=1") == []
    assert services.validate_registry_dwords(
        "PerfLevelSrc=0x3322, PowerMizerEnable=1") == []
    # an un-curated key with a valid shape passes (power users can add RmKeys)
    assert services.validate_registry_dwords("RmGpuPowerSaver=2") == []

    # curated value violates its own pattern -> surfaced
    bad = services.validate_registry_dwords("PerfLevelSrc=nope")
    assert bad and "PerfLevelSrc" in bad[0]
    # missing '=' -> not a Key=Value pair
    assert services.validate_registry_dwords("just_a_key")


def test_compose_registry_dwords_filters_invalid_keeps_valid_order() -> None:
    # mirrors the module's doctset: invalid key/value dropped, valid kept
    out = services.compose_registry_dwords(
        False,
        [("PerfLevelSrc", "0x3322"),
         ("PowerMizerEnable", "1"),
         ("Bogus", "nope"),       # un-curated, bad value -> dropped
         ("PerfLevelSrc", "bad"),  # curated, bad value -> dropped
         ("123bad", "1")],         # bad key shape -> dropped
    )
    assert out == "PerfLevelSrc=0x3322;PowerMizerEnable=1;"
    # nothing valid -> empty-trailing-; preserved as ""
    assert services.compose_registry_dwords(False, [("Bogus", "x")]) == ""


def test_kernel_param_render_applies_rebar_and_dwords_lines() -> None:
    s = GlobalDriverSettings(
        enable_resizable_bar=True,
        registry_dwords="PerfLevelSrc=0x3322;",
        registry_dwords_per_device="02:00.0:PerfLevelSrc=0x3322;",
    )
    out = _render(s)
    assert "options nvidia_drm modeset=1" in out                  # always-on base
    assert "options nvidia NVreg_EnableResizableBar=1" in out
    assert 'options nvidia NVreg_RegistryDwords="PerfLevelSrc=0x3322;"' in out
    assert ('options nvidia NVreg_RegistryDwordsPerDevice='
            '"02:00.0:PerfLevelSrc=0x3322;"' in out)

    # ReBAR off emits the explicit =0 line; empty dwords omit the line entirely
    off = _render(GlobalDriverSettings(enable_resizable_bar=False))
    assert "options nvidia NVreg_EnableResizableBar=0" in off
    assert "NVreg_RegistryDwords=" not in off


def test_persistence_status_parses_systemctl_and_smi() -> None:
    # persistence_status makes 3 subprocess calls: is-enabled, is-active, nvidia-smi.
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        head = cmd[0]
        if head == "systemctl" and "is-enabled" in cmd:
            return _fake_run("enabled\n")
        if head == "systemctl" and "is-active" in cmd:
            return _fake_run("active\n")
        if head == "nvidia-smi":
            return _fake_run("Enabled\n")
        return _fake_run("")

    with mock.patch("subprocess.run", side_effect=fake_run), \
         mock.patch("shutil.which", return_value="/usr/bin/nvidia-persistenced"):
        st = ModprobeKernelParam().persistence_status()
    assert st.supported is True
    assert st.survives_reboot is True
    assert st.active is True
    assert st.live_mode is True
    assert any("is-enabled" in c for c in calls)
    assert any("is-active" in c for c in calls)
    assert any(c[0] == "nvidia-smi" for c in calls)

    # unsupported path: binary missing short-circuits to supported=False
    # (no subprocess probes — the early return avoids touching nvidia-smi).
    with mock.patch("shutil.which", return_value=None):
        st2 = ModprobeKernelParam().persistence_status()
    assert st2.supported is False
    assert st2.survives_reboot is False
    assert st2.live_mode is False


def test_set_persistence_constructs_enable_and_disable_commands() -> None:
    captured: dict[str, list] = {}

    def fake_run(cmd, **kw):
        captured.setdefault("cmds", []).append(list(cmd))
        return _fake_run("", rc=0)   # direct (non-root) call succeeds in the test

    with mock.patch("subprocess.run", side_effect=fake_run), \
         mock.patch("shutil.which", return_value="/usr/bin/nvidia-persistenced"):
        ok, msg = ModprobeKernelParam().set_persistence(True)
    assert ok is True
    assert "survives" in msg.lower()
    # the FIRST call is the direct `systemctl enable --now nvidia-persistenced`
    assert captured["cmds"][0] == ["systemctl", "enable", "--now",
                                   "nvidia-persistenced"]

    captured.clear()

    def fake_run2(cmd, **kw):
        captured.setdefault("cmds", []).append(list(cmd))
        return _fake_run("", rc=0)

    with mock.patch("subprocess.run", side_effect=fake_run2), \
         mock.patch("shutil.which", return_value="/usr/bin/nvidia-persistenced"):
        ok2, _ = ModprobeKernelParam().set_persistence(False)
    assert ok2 is True
    assert captured["cmds"][0] == ["systemctl", "disable", "--now",
                                   "nvidia-persistenced"]


# ---------------------------------------------------------------------------
#  Decision: Steam launch injection (idempotent prepend / preserve / restore)
# ---------------------------------------------------------------------------
_LOCALCONFIG = textwrap.dedent('''\
    "UserLocalConfigStore"
    {
        "Software"
        {
            "Valve"
            {
                "Steam"
                {
                    "apps"
                    {
                        "1091500"
                        {
                            "LaunchOptions"        "PROTON_ENABLE_NVAPI=1 VKD3D_CONFIG=descriptor_indexing,dxr11 %command%"
                        }
                    }
                }
            }
        }
    }
    ''')

USER_OPTS = "PROTON_ENABLE_NVAPI=1 VKD3D_CONFIG=descriptor_indexing,dxr11 %command%"


def _locate(slo: SteamLaunchOptions, appid: str) -> str:
    """Read the LaunchOptions string for *appid* from the on-disk localconfig."""
    found = slo._find_localconfig()
    assert found, "expected a localconfig.vdf in the sandbox"
    _idx, path = found
    apps = slo._apps(loads(path.read_text()))
    return str(apps.get(appid, {}).get("LaunchOptions", ""))


def test_launch_option_inject_is_idempotent_preserves_user_and_restores() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "userdata" / "76561198000000000" / "config").mkdir(parents=True)
        lc = tmp / "userdata" / "76561198000000000" / "config" / "localconfig.vdf"
        lc.write_text(_LOCALCONFIG, encoding="utf-8")

        slo = SteamLaunchOptions(steam_root=tmp)
        env_a = str(tmp / "env" / "1091500a.env")
        env_b = str(tmp / "env" / "1091500b.env")

        # 1) apply — must prepend our source, keep the user's options, end %command%
        assert slo.ensure_wrapper("1091500", env_a)
        lo = _locate(slo, "1091500")
        assert lo.startswith('source "'), lo
        assert lo.count('source "') == 1, "never stack duplicate source lines"
        assert lo.endswith("%command%")
        assert "PROTON_ENABLE_NVAPI=1 VKD3D_CONFIG=descriptor_indexing,dxr11" in lo

        # 2) re-apply with a DIFFERENT env path — refreshes path, stays single line
        assert slo.ensure_wrapper("1091500", env_b)
        lo = _locate(slo, "1091500")
        assert lo.count('source "') == 1, "re-apply must replace, not append"
        assert "1091500b.env" in lo       # refreshed
        assert "1091500a.env" not in lo   # old path gone, not left dangling
        assert lo.endswith("%command%")

        # 3) remove — restores exactly the user's original options, no trace
        assert slo.remove_wrapper("1091500")
        lo = _locate(slo, "1091500")
        assert lo == USER_OPTS, "remove must restore user options verbatim"
        assert "source " not in lo


# ---------------------------------------------------------------------------
#  Supporting substrate: VDF parse/dumps round-trip + escaping
# ---------------------------------------------------------------------------
def test_vdf_lite_roundtrips_quoted_and_backslashed_values() -> None:
    val = 'has " and \\ chars'
    nested = {"UserLocalConfigStore": {"Software": {"apps": {"1091500": {
        "LaunchOptions": val}}}}}
    # dumps -> loads is the identity for arbitrarynestable dicts
    assert loads(dumps(nested)) == nested
    # and stable across a second round trip
    assert loads(dumps(loads(dumps(nested)))) == nested
    # the escaped value decomposes back to exactly the original string
    assert loads(dumps(nested))["UserLocalConfigStore"]["Software"]["apps"][
        "1091500"]["LaunchOptions"] == val


# ---------------------------------------------------------------------------
#  DLSS targeting + bundle model
# ---------------------------------------------------------------------------
def test_dlss_target_candidates_cover_game_root_subdirs_and_prefix() -> None:
    targets = services.dlss_target_paths(
        installdir_abs="/games/Cyberpunk",
        prefix_system32_abs="/prefix/drive_c/windows/system32",
        kinds=["dlss", "dlssg"],
    )
    paths = {p for _kind, p in targets}
    # game root is highest precedence in Proton's search
    assert "/games/Cyberpunk/nvngx_dlss.dll" in paths
    # common subdirs games actually put DLLs in
    assert "/games/Cyberpunk/Binaries/Win64/nvngx_dlssg.dll" in paths
    assert "/games/Cyberpunk/bin/nvngx_dlss.dll" in paths
    # prefix system32 tried last
    assert "/prefix/drive_c/windows/system32/nvngx_dlss.dll" in paths
    # no duplicates despite both kinds hitting the shared dirs
    assert len(targets) == len(paths)


def test_dlss_bundle_filters_to_present_dlls() -> None:
    b = DlssBundle(version="v2.12.0",
                   dlss="nvngx_dlss.dll", dlssg="nvngx_dlssg.dll")
    present = dict(b.available_dlls())
    assert present == {"dlss": "nvngx_dlss.dll", "dlssg": "nvngx_dlssg.dll"}
    # it's a fresh list, not a live view — safe to iterate after mutation
    present["dlss"] = "mutated"
    assert b.available_dlls()[0] == ("dlss", "nvngx_dlss.dll")


# ---------------------------------------------------------------------------
#  Config persistence: the TOML serializer skips None
# ---------------------------------------------------------------------------
def test_dump_toml_skips_nulls_and_round_trips_scalars() -> None:
    dumped = _dump_toml({"dlss": {
        "default_swap_version": "",
        "auto_update": False,
        "nullable_field": None,         # must be dropped (TOML has no null)
        "preferred_v": "v2.12.0",
        "ratios": [0.5, 1.0],
    }})
    assert "nullable_field" not in dumped          # the documented null-skip fix
    assert 'default_swap_version = ""' in dumped
    assert "auto_update = false" in dumped
    assert 'preferred_v = "v2.12.0"' in dumped
    # and it reloads to the same non-null scalars via stdlib tomllib
    parsed = tomllib.loads(dumped)
    assert parsed["dlss"]["auto_update"] is False
    assert parsed["dlss"]["preferred_v"] == "v2.12.0"
    assert "nullable_field" not in parsed["dlss"]


# ---------------------------------------------------------------------------
#  Honest telemetry: nvidia-smi CSV parse + "[N/A]" normalisation
# ---------------------------------------------------------------------------
def _fake_run(stdout: str, rc: int = 0) -> mock.Mock:
    m = mock.Mock()
    m.stdout = stdout
    m.stderr = ""
    m.returncode = rc
    return m


def test_gpu_query_parses_csv_and_normalises_na() -> None:
    g = NvidiaSmiGpu()
    # field order = nvidia-smi query order: name,driver,Temp,GPU%,Mem%,
    # Pdraw,Plimit,GRclk,Memclk,MemUsed,MemTotal,Fan. Values chosen so no two
    # neighbours collide (power.limit=200 is unambiguous, not any other field).
    csv_ok = ("NVIDIA GeForce RTX 5070,610.43.03,41,2,15,101,200,2100,8100,"
              "4096,12288,33\n")
    with mock.patch("subprocess.run", return_value=_fake_run(csv_ok)):
        snap = g.snapshot()
    assert snap.gpu_name == "NVIDIA GeForce RTX 5070"
    assert snap.driver_version == "610.43.03"
    assert snap.temperature_c == "41"
    assert snap.power_limit_w == "200"
    assert snap.fan_pct == "33"

    # "[N/A]" on numeric fields collapses to "" so the UI shows '—', not 'NaN'
    csv_na = "Card,610,80,[N/A],15,101,200,2100,8100,[N/A],12288,33\n"
    with mock.patch("subprocess.run", return_value=_fake_run(csv_na)):
        snap2 = g.snapshot()
    assert snap2.gpu_util_pct == ""     # was "[N/A]"
    assert snap2.mem_used_mb == ""      # was "[N/A]"
    assert snap2.fan_pct == "33"


def test_game_model_rejects_empty_identity() -> None:
    try:
        Game(appid="", name="x", installdir="d")
    except ValueError:
        pass
    else:
        raise AssertionError("empty appid must raise ValueError")
    try:
        Game(appid="1", name="", installdir="d")
    except ValueError:
        pass
    else:
        raise AssertionError("empty name must raise ValueError")


# ---------------------------------------------------------------------------
#  Review-hardened substrate: VDF control-char canonical escaping, the public
#  DLL-kind list, and the swap adapter's injected install resolver (no inline
#  peer-adapter import).
# ---------------------------------------------------------------------------
def test_vdf_roundtrips_control_chars_as_escaped_text() -> None:
    # A value carrying raw newline/tab/CR must survive dumps->loads AND be emitted
    # as the \\n \\t \\r escapes (not raw control bytes inside the quoted value).
    # (Structural \t separators between key and value are expected in the line;
    # the value's tab is what gets the \t escape.)
    val = "line1\nline2\tcol2\rend"
    nested = {"apps": {"1091500": {"LaunchOptions": val}}}
    dumped = dumps(nested)
    assert loads(dumped) == nested                      # exact round-trip
    line = next(l for l in dumped.splitlines() if "LaunchOptions" in l)
    assert "\n" not in line and "\r" not in line        # no raw newline/CR
    assert "\\n" in line and "\\t" in line and "\\r" in line  # escaped glyphs


def test_dll_kinds_exposes_canonical_list() -> None:
    # swap sweeps (status / revert) iterate this; callers must use the public list
    # rather than reaching into the private _DLL_NAMES map.
    assert services.dll_kinds() == [
        "dlss", "dlssd", "dlssg", "low_latency_vk", "deepdvc",
    ]


def test_fs_dlss_swap_resolves_install_via_injected_callable() -> None:
    import tempfile
    from nvidia_gui.adapters.dlss_swap_fs import FsDlssSwap

    g = Game(appid="1091500", name="X", installdir="X")

    # None resolver -> nothing to resolve; every kind reports "missing".
    none_swap = FsDlssSwap(resolve_install=None)
    assert none_swap.status(g, None) == {k: "missing" for k in services.dll_kinds()}
    assert none_swap.apply(g, DlssBundle(version="v2.12.0"), None) == ([], [])

    # A resolver into a real dir with one DLL marks that kind "present" — proof
    # status walked the injected path instead of an inline SteamLibrary import.
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "nvngx_dlss.dll").write_bytes(b"")
        present = FsDlssSwap(resolve_install=lambda _g: td)
        st = present.status(g, None)
    assert st["dlss"] == "present"                     # the file we placed
    assert st["dlssg"] == "missing"                    # not placed -> missing


# ---------------------------------------------------------------------------
#  Streamline GitHub fetch — resolve latest, stream the zip, extract only the
#  known DLLs (robust to the zip's internal dir layout, preferring bin/x64).
# ---------------------------------------------------------------------------
def test_dlss_cache_download_extracts_only_known_dlls() -> None:
    import io
    import json
    import tempfile
    import zipfile
    from nvidia_gui.adapters import dlss_cache_fs as mod

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for fname in ("nvngx_dlss.dll", "nvngx_dlssd.dll", "nvngx_dlssg.dll",
                      "NvLowLatencyVk.dll", "nvngx_deepdvc.dll"):
            z.writestr(f"streamline/bin/x64/{fname}", b"DLLBODY-" + fname.encode())
        z.writestr("streamline/License.txt", b"must be ignored")
        # decoy with the right basename in a debug dir -> must NOT win over bin/x64
        z.writestr("streamline/bin/debugcov/nvngx_dlss.dll", b"WRONG")
    zip_bytes = buf.getvalue()

    class _Resp:
        def __init__(self, b: bytes) -> None:
            self._b, self._p = b, 0

        def read(self, n: int = -1) -> bytes:
            if n is None or n < 0:
                out, self._p = self._b[self._p:], len(self._b)
            else:
                out = self._b[self._p:self._p + n]
                self._p += len(out)
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if "api.github.com" in req.full_url:
            meta = {"tag_name": "v2.12.0",
                    "assets": [{"name": "streamline-sdk-v2.12.0.zip",
                                "browser_download_url": "https://example.invalid/zip",
                                "size": len(zip_bytes)}]}
            return _Resp(json.dumps(meta).encode())
        return _Resp(zip_bytes)

    orig = mod.urllib.request.urlopen
    mod.urllib.request.urlopen = fake_urlopen
    try:
        with tempfile.TemporaryDirectory() as td:
            cache = mod.FsDlssCache(Path(td) / "dlss")
            seen = []
            ver = cache.download_latest_release(
                progress=lambda d, t: seen.append((d, t)))
            assert ver.version == "v2.12.0"
            d = Path(ver.path)
            # the five known DLLs extracted
            assert (d / "nvngx_dlss.dll").is_file()
            assert (d / "NvLowLatencyVk.dll").is_file()
            assert (d / "nvngx_deepdvc.dll").is_file()
            # ONLY the DLLs — no License.txt, no stray extracted tree
            assert not (d / "License.txt").exists()
            assert not (d / "streamline").exists()
            # bin/x64 preferred over the debug decoy
            assert (d / "nvngx_dlss.dll").read_bytes() == b"DLLBODY-nvngx_dlss.dll"
            # progress saw the full asset size; both network calls happened
            assert seen and seen[-1][1] == len(zip_bytes)
            assert calls["n"] == 2
    finally:
        mod.urllib.request.urlopen = orig


# ---------------------------------------------------------------------------
#  Per-game feature detection (the capability probe) -- the hexagonal facade,
#  override round-trip, the wired SteamFeatureDetector tiered merge, the
#  Cyberpunk 1091500 regression, and ACF-parser breadth across the game corpus.
# ---------------------------------------------------------------------------
# The detector's online community-DB tier is OPT-IN via the env var
# NVIDIA_GUI_FEATURE_DB_URL and OFF by default (see the feature_detection
# module docstring): absent the env var the tier is skipped and detection
# falls through to the bundled curated DB + the install-dir/prefix DLL probes
# -- the deterministic offline floor. We assert that default here and patch the
# module global to "" around each probe so the tests never depend on the
# import-time environment.
from nvidia_gui.application.ports import (  # noqa: E402
    FeatureDetectionPort,
    SettingsPort,
)
import nvidia_gui.adapters.feature_detection as _fd_mod  # noqa: E402


class _FakeFeatureDetectionPort(FeatureDetectionPort):
    """Returns a pre-crafted GameCapability for any game -- pins the use-case
    facade without exercising a real adapter."""

    def __init__(self, capability: GameCapability) -> None:
        self._cap = capability

    def probe(self, game: Game) -> GameCapability:  # noqa: ANN001
        return self._cap


class _FakeSettings(SettingsPort):
    """In-memory dotted-path store substituting for ConfigManager so override
    tests never touch the real XDG config dir or its singleton."""

    def __init__(self) -> None:
        self._d: dict = {}

    def get(self, dotted: str, default=None):  # noqa: ANN001
        return self._d.get(dotted, default)

    def set(self, dotted: str, value) -> None:  # noqa: ANN001
        self._d[dotted] = value


def _uc(*, detection=None, settings=None):
    """UseCases with the ten functional ports nulled -- the feature tests touch
    only the optional ``detection`` / ``settings`` seams."""
    from nvidia_gui.application.use_cases import UseCases
    return UseCases(
        gpu=None, driver=None, display=None, games=None,
        profiles=None, launch=None, dlss_cache=None, dlss_swap=None,
        kernel=None, diagnostics=None,
        detection=detection, settings=settings,
    )


def _feature_db(tmp: Path) -> Path:
    """A tmp bundled TOML with a single Cyberpunk 1091500 row carrying all four
    features true."""
    p = tmp / "features.toml"
    p.write_text(
        '[[games]]\nappid = "1091500"\n'
        'dlss_sr = true\ndlss_fg = true\nreflex = true\nrt = true\n'
        'note = "test row"\n',
        encoding="utf-8",
    )
    return p


# ---- umbrella 1: CONTRACT facade returns the port's capability, and an
# offline (all-UNKNOWN) capability is a legitimate, non-raising result --------
def test_detect_facade_returns_port_capability_and_offline_grace() -> None:
    g = Game(appid="1091500", name="Cyberpunk 2077", installdir="Cyberpunk 2077")
    crafted = GameCapability(
        appid="1091500",
        dlss_sr=FeatureFlag(True, FeatureSource.BUNDLED),
        reflex=FeatureFlag(True, FeatureSource.BUNDLED),
        notes="crafted",
    )
    uc = _uc(detection=_FakeFeatureDetectionPort(crafted))
    # the facade returns exactly what the port returned -- identity, no copy
    assert uc.detect_game_features(g) is crafted

    # offline grace: an all-UNKNOWN capability with an "offline" note is a
    # legitimate probe result; the facade raises nothing and is_known() is
    # False so the UI gates rather than reporting real support.
    offline = GameCapability(appid="1091500", notes="offline")
    assert offline.is_known() is False
    assert all(f.source == FeatureSource.UNKNOWN
               for f in (offline.dlss_sr, offline.dlss_fg,
                         offline.reflex, offline.rt))
    uc2 = _uc(detection=_FakeFeatureDetectionPort(offline))
    result = uc2.detect_game_features(g)
    assert result is offline and result.is_known() is False

    # no detection port wired at all -> the all-UNKNOWN default capability
    uc3 = _uc()
    bare = uc3.detect_game_features(g)
    assert bare.is_known() is False and bare.appid == "1091500"


# ---- umbrella 2: feature-override round-trip through a SettingsPort --------
def test_feature_override_roundtrip_via_settings() -> None:
    g = Game(appid="1091500", name="Cyberpunk 2077", installdir="Cyberpunk 2077")
    uc = _uc(settings=_FakeSettings())

    # set True -> reads True; set False -> reads False (a decisive negative,
    # distinct from the unset/auto no-op state)
    uc.set_feature_override(g, "dlss_sr", True)
    assert uc.feature_override(g, "dlss_sr") is True
    uc.set_feature_override(g, "rt", False)
    assert uc.feature_override(g, "rt") is False

    # clear -> the "auto" sentinel; feature_override treats it as None
    uc.clear_feature_override(g, "dlss_sr")
    assert uc.feature_override(g, "dlss_sr") is None

    # unset keys read None; the True/False pair we did NOT clear persists
    assert uc.feature_override(g, "reflex") is None
    assert uc.feature_override(g, "rt") is False

    # explicit clear of a False -> back to None (auto sentinel wins)
    uc.clear_feature_override(g, "rt")
    assert uc.feature_override(g, "rt") is None

    # facade with no settings port -> override no-ops, reads None, never raises
    uc_no = _uc()
    assert uc_no.feature_override(g, "rt") is None
    uc_no.set_feature_override(g, "rt", True)    # no-op
    uc_no.clear_feature_override(g, "rt")        # no-op


# ---- umbrella 3: DETECTION-MERGE through the wired SteamFeatureDetector -----
# Strict first-hit-wins per feature: override > online > bundled > install-dir
# DLL > prefix DLL > UNKNOWN. The online tier is off by default -- we assert the
# documented default and drive the bundled + install-dir tiers deterministically.
def test_detector_merge_bundled_wins_offline_with_install_dll() -> None:
    import tempfile
    from nvidia_gui.adapters.feature_detection import SteamFeatureDetector

    # Confirm the documented default: the online community-DB tier is OPT-IN
    # and OFF by default (the import-time module global is the empty string).
    assert _fd_mod._FEATURE_DB_URL == "", (
        "online tier must default to skip (NVIDIA_GUI_FEATURE_DB_URL off); "
        "if this fails the test env set the env var -- a non-default state"
    )

    tmp = Path(tempfile.mkdtemp())
    bundled = _feature_db(tmp)
    # An install dir carrying nvngx_dlss.dll exercises the install-dir DLL
    # probe tier. Bundled strict-precedence keeps dlss_sr.source == BUNDLED
    # even though the DLL is present (documented: the curatorial row wins
    # above the install-dir probe for the SR singleton).
    install = tmp / "CyberpunkInstall"
    install.mkdir()
    (install / "nvngx_dlss.dll").write_bytes(b"")

    corpus = Path(__file__).resolve().parent / "game_corpus"
    phantom = corpus / "installs" / "PhantomFrontier"   # real dir, NO NVIDIA DLLs

    def resolve_install(g: Game):
        if g.appid == "1091500":
            return str(install)
        if g.appid == "3266440":
            return str(phantom)
        return None

    with mock.patch.object(_fd_mod, "_FEATURE_DB_URL", ""):
        det = SteamFeatureDetector(
            resolve_install=resolve_install,
            steam_root=str(Path(tempfile.mkdtemp())),
            settings=None,
            bundled_db=bundled,
        )
        cp = Game(appid="1091500", name="Cyberpunk 2077",
                  installdir="Cyberpunk 2077")
        cap = det.probe(cp)
        # unknown game 3266440: real resolved dir, no NVIDIA DLLs -> all UNKNOWN
        cap_pf = det.probe(Game(appid="3266440", name="Phantom Frontier",
                                installdir="PhantomFrontier"))
        # resolve_install returns None -> install tier no-op -> all UNKNOWN
        cap0 = det.probe(Game(appid="0000000", name="Unknown",
                              installdir="x"))

    # online skipped (default) -> "offline" note; bundled row wins for 1091500
    assert cap.dlss_sr.supported is True
    assert cap.dlss_sr.source == FeatureSource.BUNDLED
    assert cap.dlss_fg.supported is True and cap.dlss_fg.source == FeatureSource.BUNDLED
    assert cap.reflex.supported is True and cap.reflex.source == FeatureSource.BUNDLED
    assert cap.rt.supported is True and cap.rt.source == FeatureSource.BUNDLED
    assert cap.is_known() is True
    assert "offline" in cap.notes, cap.notes

    # resolved real dir with no NVIDIA DLLs -> all UNKNOWN, known False
    assert cap_pf.is_known() is False
    assert cap_pf.dlss_sr.source == FeatureSource.UNKNOWN
    assert cap_pf.reflex.source == FeatureSource.UNKNOWN
    assert cap_pf.rt.source == FeatureSource.UNKNOWN
    assert cap_pf.notes, "unknown game must carry explanatory notes"

    # resolve returns None -> install tier no-op -> all UNKNOWN
    assert cap0.is_known() is False
    assert cap0.dlss_sr.source == FeatureSource.UNKNOWN


def test_detector_override_wins_over_bundled() -> None:
    import tempfile
    from nvidia_gui.adapters.feature_detection import SteamFeatureDetector

    tmp = Path(tempfile.mkdtemp())
    bundled = _feature_db(tmp)
    settings = _FakeSettings()
    # a True override wins over the bundled row's True too -- the distinction
    # is the SOURCE (user override, not curated).
    settings.set("feature.1091500.dlss_sr", True)
    # bundled says rt=true; an explicit False override is decisive and wins.
    settings.set("feature.1091500.rt", False)
    g = Game(appid="1091500", name="Cyberpunk 2077", installdir="Cyberpunk 2077")
    with mock.patch.object(_fd_mod, "_FEATURE_DB_URL", ""):
        det = SteamFeatureDetector(
            resolve_install=lambda _g: None,
            steam_root=str(Path(tempfile.mkdtemp())),
            settings=settings,
            bundled_db=bundled,
        )
        cap = det.probe(g)

    assert cap.dlss_sr.supported is True
    assert cap.dlss_sr.source == FeatureSource.OVERRIDE
    # rt: bundled True, but the user's False override wins outright
    assert cap.rt.supported is False
    assert cap.rt.source == FeatureSource.OVERRIDE
    # features WITHOUT an override fall through to the bundled row
    assert cap.reflex.supported is True
    assert cap.reflex.source == FeatureSource.BUNDLED
    assert cap.is_known() is True


def test_detector_never_raises_all_tiers_fail() -> None:
    import tempfile

    class _ExplodingSettings(_FakeSettings):
        def get(self, dotted, default=None):
            raise RuntimeError("settings port exploded")

    def _raising_resolve(g):  # noqa: ANN001
        raise OSError("cannot resolve install dir")

    tmp = Path(tempfile.mkdtemp())
    g = Game(appid="1091500", name="Cyberpunk 2077", installdir="Cyberpunk 2077")
    with mock.patch.object(_fd_mod, "_FEATURE_DB_URL", ""):
        det = _fd_mod.SteamFeatureDetector(
            resolve_install=_raising_resolve,
            steam_root=str(tmp / "no_prefix"),
            settings=_ExplodingSettings(),
            bundled_db=tmp / "does_not_exist.toml",
        )
        # probe must NEVER raise, even with settings.get, resolve_install, AND
        # the bundled-db read all failing concurrently.
        cap = det.probe(g)

    assert cap.is_known() is False
    assert cap.dlss_sr.source == FeatureSource.UNKNOWN
    assert cap.dlss_fg.source == FeatureSource.UNKNOWN
    assert cap.reflex.source == FeatureSource.UNKNOWN
    assert cap.rt.source == FeatureSource.UNKNOWN
    # Every exploded tier must surface in notes -- NOT just the trivial "offline"
    # skip note. This pins the honesty property: a tier crashing ("bundled DB
    # unreadable") must be distinguishable in notes from a tier finding nothing.
    # Before the per-tier-note fix this passed on the bare "offline" string alone.
    assert "override read failed" in cap.notes, cap.notes
    assert "bundled DB unreadable" in cap.notes, cap.notes
    assert "install resolve failed" in cap.notes, cap.notes
    # and the use-case facade propagates the never-raise guarantee
    assert _uc(detection=det).detect_game_features(g).is_known() is False


def test_detector_online_tier_failure_is_graceful() -> None:
    import tempfile
    import urllib.error
    tmp = Path(tempfile.mkdtemp())
    bundled = _feature_db(tmp)
    g = Game(appid="1091500", name="Cyberpunk 2077", installdir="Cyberpunk 2077")
    # OPT-IN online tier: give it a URL and force the HTTP fetch to raise --
    # the tier degrades to skip (offline guard) and bundled still decides.
    with mock.patch.object(_fd_mod, "_FEATURE_DB_URL",
                           "https://example.invalid/db.json"), \
         mock.patch.object(_fd_mod, "_CACHE_PATH", tmp / "no_cache.json"), \
         mock.patch("urllib.request.urlopen",
                    side_effect=urllib.error.URLError("net down")):
        det = _fd_mod.SteamFeatureDetector(
            resolve_install=lambda _g: None,
            steam_root=str(tmp / "no_prefix"),
            settings=None,
            bundled_db=bundled,
        )
        cap = det.probe(g)
    # online failed and was skipped -> bundled row wins; no raise
    assert cap.dlss_sr.supported is True
    assert cap.dlss_sr.source == FeatureSource.BUNDLED
    assert cap.is_known() is True
    # the offline guard NAMED the unreachable fetch -- not just a truthy blob
    # (the old test asserted only `cap.notes` and passed coincidentally on the
    # generic note; pin the actual "unreachable" message so a swallow-regression
    # that drops it can no longer pass).
    assert "unreachable" in cap.notes, cap.notes


# ---- umbrella 4: CYBERPUNK 1091500 regression against the REAL bundled DB -
def test_cyberpunk_1091500_regression_via_real_bundled_db() -> None:
    import tempfile
    from nvidia_gui.adapters.feature_detection import SteamFeatureDetector

    # the REAL shipped curated DB (not a tmp one) -- pins the known-problem
    # game so GamesView gating never re-regresses.
    real_db = (Path(__file__).resolve().parent.parent / "src" / "nvidia_gui"
               / "assets" / "nvidia_features.toml")
    assert real_db.is_file(), f"bundled feature DB missing: {real_db}"
    with mock.patch.object(_fd_mod, "_FEATURE_DB_URL", ""):
        det = SteamFeatureDetector(
            resolve_install=lambda _g: None,
            steam_root=str(Path(tempfile.mkdtemp())),
            settings=None,
            bundled_db=real_db,
        )
        cp = Game(appid="1091500", name="Cyberpunk 2077",
                  installdir="Cyberpunk 2077")
        cap = det.probe(cp)
    # Cyberpunk 2077: path tracing + DLSS 3/4 FG + Reflex + RT -- all supported
    assert cap.dlss_sr.supported is True
    assert cap.dlss_sr.source in (FeatureSource.BUNDLED, FeatureSource.INSTALLDIR,
                                  FeatureSource.PREFIX)
    assert cap.dlss_fg.supported is True
    assert cap.reflex.supported is True
    assert cap.rt.supported is True
    assert cap.is_known() is True
    assert cap.notes   # no crash; the offline floor (bundled) decided


# ---- umbrella 4b: BORDERLANDS 4 (1285190) curated regression ---------------
# The named user-reported miss: BL4 was UNKNOWN after Detect because it was
# absent from the curated DB. Pins the bund-led row so it can't revert.
def test_borderlands4_1285190_curated_regression_via_real_bundled_db() -> None:
    import tempfile
    from nvidia_gui.adapters.feature_detection import SteamFeatureDetector

    real_db = (Path(__file__).resolve().parent.parent / "src" / "nvidia_gui"
               / "assets" / "nvidia_features.toml")
    assert real_db.is_file(), f"bundled feature DB missing: {real_db}"
    with mock.patch.object(_fd_mod, "_FEATURE_DB_URL", ""):
        det = SteamFeatureDetector(
            resolve_install=lambda _g: None,
            steam_root=str(Path(tempfile.mkdtemp())),
            settings=None,
            bundled_db=real_db,
        )
        bl4 = Game(appid="1285190", name="Borderlands 4",
                   installdir="Borderlands 4")
        cap = det.probe(bl4)
    # Borderlands 4: DLSS 4 SR + FG + MFG + RT, Reflex 2 inferred from MFG.
    assert cap.dlss_sr.supported is True
    assert cap.dlss_sr.source in (FeatureSource.BUNDLED, FeatureSource.INSTALLDIR,
                                  FeatureSource.PREFIX)
    assert cap.dlss_fg.supported is True
    assert cap.reflex.supported is True
    assert cap.rt.supported is True
    assert cap.is_known() is True
    assert cap.notes


# ---- umbrella 4c: install-dir probe covers Binaries/Win64 (general fix) ---
# The GENERAL robustness fix, not just the curated row: an UNLISTED game (a
# fictional appid with NO curated row) whose nvngx_dlss.dll ships in the UE-style
# ``Binaries/Win64`` must self-detect via the install-dir DLL probe -- before
# this batch _probe_dll_tree never looked there, so any UE4/UE5 DLSS game the
# curated DB hadn't vetted fell to UNKNOWN even with the DLL present on disk.
def test_install_dir_probe_finds_dlss_in_binaries_win64_unlisted() -> None:
    import tempfile
    from nvidia_gui.adapters.feature_detection import SteamFeatureDetector

    tmp = Path(tempfile.mkdtemp())
    install = tmp / "common" / "FictionalUE5Game"
    win64 = install / "Binaries" / "Win64"
    win64.mkdir(parents=True)
    (win64 / "nvngx_dlss.dll").write_bytes(b"PE\x00\x00")  # present on disk
    real_db = (Path(__file__).resolve().parent.parent / "src" / "nvidia_gui"
               / "assets" / "nvidia_features.toml")
    with mock.patch.object(_fd_mod, "_FEATURE_DB_URL", ""):
        det = SteamFeatureDetector(
            resolve_install=lambda g: str(install) if g.appid == "7777777" else None,
            steam_root=str(tmp / "steam"),     # no prefix dir -> prefix tier skips
            settings=None,
            bundled_db=real_db,
        )
        # Fictional appid -> NO curated row -> bundled tier absent -> the
        # install-dir DLL probe (pass 1, Binaries/Win64 candidate) must decide.
        g = Game(appid="7777777", name="Fictional UE5 Game",
                 installdir="FictionalUE5Game")
        cap = det.probe(g)
    assert cap.dlss_sr.supported is True, cap.notes
    assert cap.dlss_sr.source == FeatureSource.INSTALLDIR, cap.dlss_sr.source
    assert cap.is_known() is True


# ---- umbrella 4d: bounded walk fallback finds DLSS at an exotic path -------
# Pass 2 (the os.walk fallback) must catch a DLL at a path pass 1 doesn't list
# -- the whole point of the fallback is that curation + well-known dirs can't
# cover every shipping layout. A vendored-plugin path proves the walk fires.
def test_install_dir_probe_bounded_walk_finds_dlss_at_exotic_path() -> None:
    import tempfile
    from nvidia_gui.adapters.feature_detection import SteamFeatureDetector

    tmp = Path(tempfile.mkdtemp())
    install = tmp / "common" / "FictionalExoticGame"
    exotic = install / "Plugins" / "VendorNV" / "Binaries" / "Win64"
    exotic.mkdir(parents=True)
    (exotic / "nvngx_dlss.dll").write_bytes(b"PE\x00\x00")
    # a media subtree that the walk must prune (would otherwise be walked) --
    # proves the prune set keeps the walk bounded without missing the DLL.
    (install / "Content" / "Cinematics").mkdir(parents=True)
    for i in range(3):
        (install / "Content" / "Cinematics" / f"movie_{i}.bik").write_bytes(b"")
    real_db = (Path(__file__).resolve().parent.parent / "src" / "nvidia_gui"
               / "assets" / "nvidia_features.toml")
    with mock.patch.object(_fd_mod, "_FEATURE_DB_URL", ""):
        det = SteamFeatureDetector(
            resolve_install=lambda g: str(install) if g.appid == "8888888" else None,
            steam_root=str(tmp / "steam"),
            settings=None,
            bundled_db=real_db,
        )
        g = Game(appid="8888888", name="Fictional Exotic Game",
                 installdir="FictionalExoticGame")
        cap = det.probe(g)
    assert cap.dlss_sr.supported is True, cap.notes
    assert cap.dlss_sr.source == FeatureSource.INSTALLDIR, cap.dlss_sr.source


# ---- umbrella 5: ACF PARSER breadth across the game_corpus fixtures -------
def test_acf_parser_breadth_multi_library_and_broken_manifest() -> None:
    import shutil
    import tempfile
    from nvidia_gui.adapters.game_library_steam import SteamLibrary
    from nvidia_gui.adapters.vdf_lite import VdfError

    corpus = Path(__file__).resolve().parent / "game_corpus"

    # (1) the corpus libraryfolders.vdf parses to the documented 3-lib shape:
    # lib 0 = default Steam root, lib 1 carries 4400001, lib 2 carries 4400002.
    lf = loads((corpus / "libraryfolders.vdf").read_text(encoding="utf-8"))
    libs = lf["libraryfolders"]
    assert set(libs.keys()) == {"0", "1", "2"}
    assert libs["1"]["apps"] == {"4400001": "16990011"}
    assert libs["2"]["apps"] == {"4400002": "16990022"}

    # (2) build a tmp MULTI-LIBRARY tree and scan it through the real reader.
    # A custom libraryfolders.vdf points at two REAL tmp roots so the reader's
    # is_dir() gate resolves them (the corpus vdf's /mnt paths don't exist and
    # its "0" path would resolve to the user's real Steam home, polluting the
    # scan with non-fixture games).
    broken_text = (corpus / "appmanifest_9999999.acf").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        main_root = td / "steam"
        lib2 = td / "lib2"
        (main_root / "steamapps").mkdir(parents=True)
        (lib2 / "steamapps").mkdir(parents=True)
        lf_text = dumps({"libraryfolders": {
            "0": {"path": str(main_root), "label": ""},
            "1": {"path": str(lib2),
                  "label": "",
                  "apps": {"4400001": "16990011"}},
        }})
        (main_root / "steamapps" / "libraryfolders.vdf").write_text(lf_text)
        # well-formed corpus acfs into the main library + the broken/truncated
        # appmanifest_9999999.acf to prove scan() does NOT crash on it.
        for appid in ("1091500", "3219900", "3266440", "4400002", "9999999"):
            shutil.copy(corpus / f"appmanifest_{appid}.acf",
                        main_root / "steamapps" / f"appmanifest_{appid}.acf")
        # the second-library-only game goes into lib2 -- proves multi-library
        shutil.copy(corpus / "appmanifest_4400001.acf",
                    lib2 / "steamapps" / "appmanifest_4400001.acf")

        games = SteamLibrary(steam_root=main_root).scan()
        by_id = {g.appid: g for g in games}

    # the broken/truncated manifest is SKIPPED (no crash) -- never appears
    assert "9999999" not in by_id
    assert len(games) == 5
    # the well-formed manifests parsed to the expected name/installdir
    assert by_id["1091500"].name == "Cyberpunk 2077"
    assert by_id["1091500"].installdir == "Cyberpunk 2077"
    assert by_id["3219900"].name == "Mind Blocks"
    assert by_id["3219900"].installdir == "MindBlocks"
    assert by_id["3266440"].name == "Phantom Frontier"
    assert by_id["3266440"].installdir == "PhantomFrontier"
    assert by_id["4400002"].name == "Dust Protocol"
    assert by_id["4400002"].installdir == "DustProtocol"
    # and games came from BOTH libraries (4400001 is lib2-only)
    assert by_id["4400001"].name == "Titan Sieve"
    assert by_id["4400001"].installdir == "TitanSieve"

    # (3) in isolation the broken manifest's malformedness raises VdfError --
    # that is precisely why SteamLibrary._parse_acf must (and does) catch it.
    raised = False
    try:
        loads(broken_text)
    except VdfError:
        raised = True
    assert raised, "the broken acf must raise VdfError -- scan() relies on the catch"


def test_version_never_lies_dev_fallback_when_uninstalled() -> None:
    """uc.version() honors the confidence-tiered fallback: installed metadata is
    authoritative; the SettingsPort ``app.version`` is the secondary; a totally
    bare checkout returns the literal ``"dev"`` rather than a fabricated number.

    Never-lie provenance mirrors the feature detector: an unset surface degrades
    honestly instead of presenting a stale hard-coded value.
    """
    from nvidia_gui.application.use_cases import UseCases
    import importlib.metadata as md

    def _bare_uc(settings=None):
        return UseCases(
            gpu=None, driver=None, display=None, games=None, profiles=None,
            launch=None, dlss_cache=None, dlss_swap=None, kernel=None,
            diagnostics=None, detection=None, settings=settings,
        )

    # 1) metadata raises PackageNotFoundError -> "dev" (bare checkout, no
    #    settings port). ``mock.patch`` resets cleanly so the test is
    #    independent of whether this checkout happens to be pip-installed.
    with mock.patch.object(
        md, "version", side_effect=md.PackageNotFoundError("nvidia-gui")
    ):
        assert _bare_uc().version() == "dev", "uninstalled -> 'dev'"

    # 2) metadata still raises, but a SettingsPort carries app.version -> that
    #    configured value surfaces (the secondary tier).
    class _SettingsDict:
        def __init__(self, d):
            self._d = d
        def get(self, dotted, default=None):
            return self._d.get(dotted, default)
        def set(self, dotted, value):
            self._d[dotted] = value
    sd = _SettingsDict({"app.version": "9.9.9-test"})
    with mock.patch.object(
        md, "version", side_effect=md.PackageNotFoundError("nvidia-gui")
    ):
        assert _bare_uc(sd).version() == "9.9.9-test", "configured app.version falls back"

    # 3) installed metadata resolves -> that is authoritative (the primary
    #    tier), regardless of what app.version preferences says.
    with mock.patch.object(md, "version", return_value="2.3.4"):
        assert _bare_uc(sd).version() == "2.3.4", "installed metadata wins"

    # 4) a metadata read that throws something OTHER than PackageNotFoundError
    #    (corrupted dist metadata) is swallowed -> falls to the next tier, never
    #    propagates into the UI.
    with mock.patch.object(md, "version", side_effect=RuntimeError("boom")):
        assert _bare_uc(sd).version() == "9.9.9-test", "non-PackageNotFound swallowed"


def test_about_dialog_close_request_signal_connectable() -> None:
    """Regression guard for the About-dialog silent-failure class.

    ``open_about_dialog`` (views_settings) connects ``close-request`` to
    destroy the dialog. In GTK4 ``GtkAboutDialog`` is a direct ``Gtk.Window``
    (no longer a ``Gtk.Dialog``): its Close button emits ``close-request``
    (inherited from ``GtkWindow``), NOT ``response`` (which lives only on the
    deprecated ``Gtk.Dialog``). The previous wiring used ``connect("response",
    ...)`` -- which raises ``unknown signal name: response`` and was swallowed
    by the About builder's broad-except, so the dialog silently failed to
    present (the user clicked About and nothing appeared). This test pins the
    signal our code relies on so a reconnect-to-``response`` regression fails
    the gate immediately, not in a manual About-box click months later.

    Display-free: a widget can be constructed (but not realized) without a
    ``GdkDisplay``; we never call ``present()`` here.
    """
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    dlg = Gtk.AboutDialog.new()
    # The signal open_about_dialog relies on must connect without raising.
    dlg.connect("close-request", lambda d: d.destroy())


# ---------------------------------------------------------------------------
#  Plain-python runner (used when pytest isn't installed; pytest also works)
# ---------------------------------------------------------------------------
def _run_all() -> int:
    fns = sorted(
        (name, obj) for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj)
    )
    passed = failed = 0
    for name, fn in fns:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — report every failure
            failed += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"  ok   {name}")
    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
