"""Read-only per-game diagnostics.

Probes the layers that decide whether a Proton game can see the NVIDIA GPU and
use RT / DLSS: the Vulkan NVIDIA ICD, vulkaninfo's RT-extension report, the
Proton tool bound to the game in ``localconfig.vdf`` (``CompatToolMapping``),
the NVAPI/DLSS DLLs present in the prefix, and the prefix's age.

Pure probing — never mutates state — and defensive: a missing ``vulkaninfo`` or
an unreadable localconfig is itself a probe result, never a raise. The verdict
names the most likely cause so the user can distinguish a *fixable env problem*
(set a toggle / rebuild the prefix) from a *Proton+driver runtime one* that no
env var will fix here.
"""

from __future__ import annotations

import logging
import pathlib
import re
import subprocess
import time

from ..application.ports import DiagnosticsPort
from ..domain.models import DiagStatus, DiagnosticCheck, DiagnosticReport, Game
from .vdf_lite import VdfError, loads

logger = logging.getLogger(__name__)

# Ray-tracing Vulkan device extensions vulkaninfo reports when RT is exposed.
_RT_EXTENSIONS = (
    "VK_KHR_ray_tracing_pipeline",
    "VK_KHR_acceleration_structure",
    "VK_KHR_ray_query",
)
# DLLs a Proton prefix should contain when NVAPI/DLSS is wired up.
_NVAPI_DLLS = ("nvapi64.dll", "nvapi.dll", "nvngx_dlss.dll", "nvngx_dlssg.dll")
_ICD_DIRS = (
    "/usr/share/vulkan/icd.d",
    "/etc/vulkan/icd.d",
    "/usr/local/share/vulkan/icd.d",
)
_USER_ICD_DIR = "~/.local/share/vulkan/icd.d"
_PREFIX_STALE_DAYS = 180
_VULKANINFO_TIMEOUT = 6.0


def _nvidia_icd_found() -> bool:
    dirs = [pathlib.Path(d) for d in _ICD_DIRS]
    dirs.append(pathlib.Path(_USER_ICD_DIR).expanduser())
    for d in dirs:
        if not d.is_dir():
            continue
        for j in d.glob("*.json"):
            try:
                txt = j.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # The NVIDIA ICD manifest names libnvidia* in its library path.
            if "nvidia" in txt.lower():
                return True
    return False


def _vulkaninfo_rt() -> tuple[list[str], list[str]] | None:
    """Run ``vulkaninfo --json`` and return (deviceNames, present_rt_exts), or
    None if the tool is absent/failed. String-matches the JSON text — robust to
    the nesting churn across vulkan-tools versions."""
    try:
        r = subprocess.run(
            ["vulkaninfo", "--json"],
            capture_output=True, text=True, timeout=_VULKANINFO_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug("vulkaninfo unavailable: %s", exc)
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    txt = r.stdout
    devs = list(dict.fromkeys(
        m.group(1) for m in re.finditer(r'"deviceName"\s*:\s*"([^"]+)"', txt)
    ))
    exts = [e for e in _RT_EXTENSIONS if e in txt]
    return devs, exts


def _find_localconfig(steam_root: pathlib.Path) -> pathlib.Path | None:
    """Newest ``userdata/<id>/config/localconfig.vdf`` by mtime (mirror of the
    launch adapter's picker, kept local so this adapter stays peer-independent)."""
    userdata = steam_root / "userdata"
    if not userdata.is_dir():
        return None
    cands = sorted(
        userdata.glob("*/config/localconfig.vdf"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for p in cands:
        try:
            int(p.parent.parent.name)
        except ValueError:
            continue
        return p
    return None


def _compat_tool(steam_root: pathlib.Path, appid: str) -> str:
    """Proton/compat-tool name bound to *appid* in localconfig.vdf ("" if unset
    or unreadable)."""
    lc = _find_localconfig(steam_root)
    if lc is None:
        return ""
    try:
        data = loads(lc.read_text(encoding="utf-8", errors="replace"))
    except (VdfError, OSError):
        return ""
    mapping = (
        data.get("UserLocalConfigStore", {})
             .get("Software", {})
             .get("Valve", {})
             .get("Steam", {})
             .get("CompatToolMapping", {})
    )
    cfg = mapping.get(str(appid)) if isinstance(mapping, dict) else None
    return str(cfg.get("name") or "") if isinstance(cfg, dict) else ""


def _prefix_dir(steam_root: pathlib.Path, appid: str) -> pathlib.Path:
    return steam_root / "steamapps" / "compatdata" / str(appid)


def _tool_version(steam_root: pathlib.Path, tool: str) -> str:
    if not tool:
        return ""
    try:
        return (steam_root / "steamapps" / "common" / tool / "version.txt"
                ).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


class SteamDiagnostics(DiagnosticsPort):
    """Per-game read-only diagnostics against a Steam library root."""

    def __init__(self, steam_root: pathlib.Path | str | None = None) -> None:
        self._steam = pathlib.Path(
            steam_root or (pathlib.Path.home() / ".local/share/Steam")
        )

    def probe(self, game: Game) -> DiagnosticReport:
        checks: list[DiagnosticCheck] = []

        # 1) Vulkan NVIDIA ICD presence (ICD manifest dirs).
        icd = _nvidia_icd_found()
        checks.append(DiagnosticCheck(
            name="Vulkan ICD (NVIDIA)",
            status=DiagStatus.OK if icd else DiagStatus.WARN,
            detail="NVIDIA Vulkan ICD manifest found" if icd
                   else "No NVIDIA ICD under /usr/share/vulkan/icd.d (etc.)",
            fix_hint="" if icd
                      else "Install the NVIDIA Vulkan ICD / loader (vulkan-tools)",
        ))

        # 2) vulkaninfo RT-extension probe (authoritative for RT exposure).
        vk = _vulkaninfo_rt()
        exts: list[str] = []
        if vk is None:
            checks.append(DiagnosticCheck(
                name="Vulkan RT extensions",
                status=DiagStatus.WARN,
                detail="vulkaninfo --json unavailable or failed",
                fix_hint="Install vulkan-tools; run `vulkaninfo --summary` manually",
            ))
        else:
            devs, exts = vk
            checks.append(DiagnosticCheck(
                name="Vulkan RT extensions",
                status=DiagStatus.OK if exts else DiagStatus.WARN,
                detail=(", ".join(exts) + " present") if exts
                       else "RT extensions NOT reported by vulkaninfo",
                fix_hint="" if exts
                          else "RTX + 610 driver should expose RT — check the Vulkan loader / DXVK",
            ))
            if devs:
                checks.append(DiagnosticCheck(
                    name="Vulkan devices",
                    status=DiagStatus.INFO,
                    detail=", ".join(devs),
                    fix_hint="",
                ))

        # 3) Proton compat tool bound to this game.
        tool = _compat_tool(self._steam, game.appid)
        ver = _tool_version(self._steam, tool)
        checks.append(DiagnosticCheck(
            name="Proton tool",
            status=DiagStatus.OK if tool else DiagStatus.WARN,
            detail=f"{tool} ({ver})" if (tool and ver)
                   else (tool or "No Proton tool mapped in localconfig.vdf (game may run native)"),
            fix_hint="" if tool
                      else "Force a Proton compatibility tool for this game in Steam",
        ))

        # 4) prefix NVAPI/DLSS DLLs + age.
        pfx = _prefix_dir(self._steam, game.appid)
        prefix_present = pfx.is_dir()
        prefix_dlls_present = False
        stale = False
        if prefix_present:
            sys32 = pfx / "pfx" / "drive_c" / "windows" / "system32"
            found = [d for d in _NVAPI_DLLS if (sys32 / d).is_file()]
            prefix_dlls_present = bool(found)
            checks.append(DiagnosticCheck(
                name="Prefix NVAPI/DLSS DLLs",
                status=DiagStatus.OK if found else DiagStatus.WARN,
                detail=(", ".join(found) + " in system32") if found
                       else "No nvapi*/nvngx_dlss DLLs in prefix — DLSS won't load",
                fix_hint="" if found
                          else "Enable DXVK-NVAPI and rebuild compatdata so NVAPI DLLs are injected",
            ))
            age_days = int((time.time() - pfx.stat().st_mtime) // 86400)
            stale = age_days > _PREFIX_STALE_DAYS
            checks.append(DiagnosticCheck(
                name="Prefix age",
                status=DiagStatus.WARN if stale else DiagStatus.OK,
                detail=f"~{age_days} days old" +
                       (" (stale — Proton may predate current RT/NVAPI fixes)" if stale else ""),
                fix_hint="Rebuild: remove compatdata/{appid} and relaunch via Steam"
                         if stale else "",
            ))
        else:
            checks.append(DiagnosticCheck(
                name="Proton prefix",
                status=DiagStatus.INFO,
                detail=f"No prefix at {pfx} (game not launched via Proton yet?)",
                fix_hint="Launch the game once via Steam/Proton to create the prefix",
            ))

        verdict = self._verdict(icd, exts, tool, prefix_present,
                                prefix_dlls_present, stale)
        return DiagnosticReport(appid=game.appid, checks=tuple(checks), verdict=verdict)

    @staticmethod
    def _verdict(icd: bool, exts: list[str], tool: str, prefix_present: bool,
                 _prefix_dlls_present: bool, stale: bool) -> str:
        """Synthesize a one-line most-likely-cause from the warn/fail signals."""
        causes: list[str] = []
        if not icd:
            causes.append("no NVIDIA Vulkan ICD")
        if not exts:
            causes.append("Vulkan exposes no RT extensions")
        if not tool:
            causes.append("no Proton tool mapped")
        if not prefix_present:
            causes.append("no Proton prefix yet")
        elif not _prefix_dlls_present:
            causes.append("prefix has no NVAPI/DLSS DLLs")
        elif stale:
            causes.append("stale compatibility prefix")
        if causes:
            return ("Most likely cause: " + "; ".join(causes)
                    + ". Apply the hints above, then relaunch via Steam.")
        return ("Environment probes look OK. If the game still can't see the GPU or "
                "RT is blocked, enable 'Expose NVIDIA GPU' + 'Force DXR' here, Save, "
                "then relaunch the game via Steam.")
