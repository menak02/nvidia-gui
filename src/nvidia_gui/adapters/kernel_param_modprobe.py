"""NVreg_* kernel-module tuning via /etc/modprobe.d/nvidia-gui.conf.

Writing to ``/etc/modprobe.d/`` needs root. We hand the generated content to
``pkexec`` (Graphical polkit prompt) when available and fall back to ``sudo``;
the install.sh ships a polkit policy so the prompt names our action. Settings
apply at the next boot — we surface that honestly rather than attempting a
fragile live-sysfs patch (NVreg_RegistryDwords is load-time for the open
module).
"""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
import subprocess
import tempfile

from ..application.ports import KernelParamPort
from ..domain import services
from ..domain.models import GlobalDriverSettings, PersistenceState

logger = logging.getLogger(__name__)

_MODPROBE_CONF = pathlib.Path("/etc/modprobe.d/nvidia-gui.conf")
_BASE_LINE = "options nvidia_drm modeset=1"
# Privileged helper installed by install.sh. When present we pkexec THIS (not
# a generic ``sh -c``) so pkexec resolves to our dedicated polkit action and
# shows our own-password prompt naming the app. Its absence (running straight
# from the repo, no install) just falls through to the generic pkexec/sudo path.
_HELPER = "/usr/libexec/nvidia-gui-apply-modprobe"
# The NVIDIA persistence daemon. Enabling this service (systemctl enable --now)
# keeps the driver loaded across accesses and survives reboot — unlike
# `nvidia-smi -pm 1`, which drops on module reload. The service name == binary.
_PERSISTENCED = "nvidia-persistenced"


def _render(settings: GlobalDriverSettings) -> str:
    lines: list[str] = ["# Managed by nvidia-gui — applies at next boot.", _BASE_LINE]
    if settings.enable_resizable_bar:
        lines.append("options nvidia NVreg_EnableResizableBar=1")
    else:
        lines.append("options nvidia NVreg_EnableResizableBar=0")
    if settings.registry_dwords:
        lines.append(f'options nvidia NVreg_RegistryDwords="{settings.registry_dwords}"')
    if settings.registry_dwords_per_device:
        lines.append(
            'options nvidia NVreg_RegistryDwordsPerDevice="'
            f'{settings.registry_dwords_per_device}"'
        )
    return "\n".join(lines) + "\n"


class ModprobeKernelParam(KernelParamPort):
    def read_config(self) -> GlobalDriverSettings:
        s = GlobalDriverSettings()
        if not _MODPROBE_CONF.is_file():
            return s
        try:
            text = _MODPROBE_CONF.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("cannot read %s: %s", _MODPROBE_CONF, exc)
            return s
        for line in text.splitlines():
            t = line.strip()
            if t.startswith("#") or not t.startswith("options"):
                continue
            if "NVreg_EnableResizableBar=1" in t:
                s.enable_resizable_bar = True
            elif "NVreg_EnableResizableBar=0" in t:
                s.enable_resizable_bar = False
            if "NVreg_RegistryDwords=" in t:
                s.registry_dwords = _quoted(t, "NVreg_RegistryDwords=")
            if "NVreg_RegistryDwordsPerDevice=" in t:
                s.registry_dwords_per_device = _quoted(t, "NVreg_RegistryDwordsPerDevice=")
        return s

    def write_config(self, settings: GlobalDriverSettings) -> tuple[bool, str]:
        # validate before we touch anything
        problems = services.validate_registry_dwords(settings.registry_dwords)
        problems += services.validate_registry_dwords(settings.registry_dwords_per_device)
        if problems:
            return False, "Validation failed:\n" + "\n".join(problems)
        content = _render(settings)
        # 1) can we write directly (dev/test boxes with loosened perms)?
        try:
            _MODPROBE_CONF.write_text(content)
            return True, "Written directly (reboot to apply)."
        except PermissionError:
            pass
        # 2) elevated via pkexec/sudo: stage a temp, then have root install it.
        return _elevated_install(content)

    def can_write_without_root(self) -> bool:
        try:
            with (_MODPROBE_CONF.parent / ".nvagogui_probe").open("w"):
                pass
            (_MODPROBE_CONF.parent / ".nvagogui_probe").unlink()
            return True
        except OSError:
            return False

    # ---- persistence mode (nvidia-persistenced service, survives reboot) ---
    def persistence_status(self) -> PersistenceState:
        if shutil.which(_PERSISTENCED) is None:
            return PersistenceState(supported=False)
        enabled = self._systemctl("is-enabled", _PERSISTENCED).strip() == "enabled"
        active = self._systemctl("is-active", _PERSISTENCED).strip() == "active"
        live = False
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=persistence_mode", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=6,
            )
            live = r.returncode == 0 and r.stdout.strip().lower() == "enabled"
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("persistence_mode probe failed: %s", exc)
        return PersistenceState(
            survives_reboot=enabled, active=active, live_mode=live, supported=True,
        )

    def set_persistence(self, enabled: bool) -> tuple[bool, str]:
        if not shutil.which(_PERSISTENCED):
            return False, f"{_PERSISTENCED} not installed; cannot manage persistence."
        verb = "enable --now" if enabled else "disable --now"
        args = ["systemctl", *verb.split(), _PERSISTENCED]
        done = ("Enabled" if enabled else "Disabled") + " nvidia-persistenced"
        on_hint = (" — survives reboot; the driver stays loaded between "
                   "accesses so the focus-transition clock re-init (the tab-in "
                   "stutter) should clear.")
        hint = on_hint if enabled else "."
        # 1) direct (only succeeds if already root) — fall through on any rc!=0,
        #    then 2) pkexec (graphical), 3) sudo (terminal). Same ladder shape as
        #    _elevated_install so the polkit/sudo behaviour matches modprobe Apply.
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                return True, done + hint
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("direct systemctl failed: %s", exc)
        for launcher, desc in (
            ("pkexec", "pkexec graphical prompt"),
            ("sudo", "sudo (terminal prompt)"),
        ):
            exe = shutil.which(launcher)
            if not exe:
                continue
            cmd = [exe, *args] if launcher == "pkexec" else [exe, "--", *args]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.warning("%s persistenced failed: %s", launcher, exc)
                continue
            if r.returncode == 0:
                return True, f"{done} via {desc}.{hint if enabled else ''}"
            logger.warning("%s rc=%s stderr=%s", launcher, r.returncode, r.stderr[:160])
        return False, (
            f"Could not elevate to {verb} {_PERSISTENCED}.\n"
            f"Run in a terminal:  sudo systemctl {verb} {_PERSISTENCED}"
        )

    @staticmethod
    def _systemctl(*args: str) -> str:
        """Read-only systemctl probe. Returns stdout on success, '' on any
        failure/timeout so callers can treat the empty result as 'not that
        state'. ``is-enabled``/``is-active`` are usable by a non-root caller."""
        try:
            r = subprocess.run(["systemctl", *args], capture_output=True,
                               text=True, timeout=8)
            return r.stdout if r.returncode == 0 else ""
        except (subprocess.TimeoutExpired, OSError):
            return ""


def _quoted(line: str, after: str) -> str:
    idx = line.find(after)
    if idx < 0:
        return ""
    rest = line[idx + len(after):].strip()
    if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
        return rest[1:-1]
    parts = rest.split()
    return parts[0] if parts else ""


def _elevated_install(content: str) -> tuple[bool, str]:
    """Stage content to a user temp file, then elevate to install it.

    Tries in order:
      1. ``pkexec /usr/libexec/nvidia-gui-apply-modprobe <staged>`` — pkexec
         resolves this to our dedicated polkit action so the dialog names the
         app and honours ``auth_self_keep``. Needs install.sh to have put the
         helper in place.
      2. ``pkexec sh -c 'cp … && chmod … && rm …; echo OK'`` — the generic
         pkexec prompt, used when the helper isn't installed (dev checkout).
      3. ``sudo sh -c …`` — terminal fallback (no polkit agent available).
    """
    fd, tmp_path = tempfile.mkstemp(prefix="nvagogui-modprobe-", suffix=".conf")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp_path, 0o644)
        dest = str(_MODPROBE_CONF)

        pkexec = shutil.which("pkexec")
        helper = (
            shutil.which("nvidia-gui-apply-modprobe")
            or (_HELPER if os.path.isfile(_HELPER) else "")
        )
        # 1) the action-aware helper — our own polkit dialog
        if pkexec and helper:
            try:
                r = subprocess.run([pkexec, helper, tmp_path],
                                   capture_output=True, text=True, timeout=60)
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.warning("pkexec helper failed: %s", exc)
            else:
                if r.returncode == 0 and "OK" in r.stdout:
                    return True, ("Applied via NVIDIA-GUI polkit action "
                                  "(own-password prompt). Reboot to take effect.")
                logger.warning("pkexec helper rc=%s stderr=%s",
                               r.returncode, r.stderr[:160])

        # 2) generic pkexec sh -c, then 3) sudo sh -c
        installer = (
            f'cp "{tmp_path}" "{dest}" && chmod 644 "{dest}" && '
            f'rm -f "{tmp_path}" && echo OK'
        )
        for launcher, desc in (
            ("pkexec", "pkexec graphical prompt (generic)"),
            ("sudo", "sudo (terminal prompt)"),
        ):
            exe = shutil.which(launcher)
            if not exe or (launcher == "pkexec" and not pkexec):
                continue
            cmd = (
                [exe, "sh", "-c", installer] if launcher == "pkexec"
                else [exe, "--", "sh", "-c", installer]
            )
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.warning("%s install failed: %s", launcher, exc)
                continue
            if r.returncode == 0 and "OK" in r.stdout:
                return True, f"Applied via {desc}. Reboot to take effect."
            logger.warning("%s rc=%s stderr=%s", launcher, r.returncode, r.stderr[:160])
        return False, (
            "Could not elevate (no pkexec/sudo, or it was cancelled).\n"
            "Run this once in a terminal:\n"
            f"  sudo install -m644 {tmp_path} {dest}"
        )
    except OSError as exc:
        return False, f"Failed to stage config: {exc}"
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
