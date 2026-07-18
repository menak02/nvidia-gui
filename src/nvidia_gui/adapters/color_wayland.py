"""Wayland adapter — Digital Brightness and Contrast display controls."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Any

from ..application.ports import ColorPort

logger = logging.getLogger(__name__)


class WaylandColorAdapter(ColorPort):
    """Synchronous Wayland adapter for brightness and contrast.

    Queries displays using `wlr-randr` and applies brightness and contrast
    using `wl-gammarelay-rs` over DBus (which works on wlroots-based compositors
    like Hyprland and Sway).
    """

    def __init__(self, settings=None) -> None:
        self._bin_randr = shutil.which("wlr-randr")
        self._bin_busctl = shutil.which("busctl")
        self._settings = settings

    def is_available(self) -> bool:
        """True if wlr-randr is available and either hyprctl (for Hyprland) or busctl (for wl-gammarelay-rs) is available."""
        if self._bin_randr is None:
            return False
        
        import os
        if shutil.which("hyprctl") is not None and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return True

        return self._bin_busctl is not None

    def _is_gammarelay_active(self) -> bool:
        """Check if wl-gammarelay-rs daemon is active on the session bus."""
        if not self._bin_busctl:
            return False
        try:
            out = subprocess.run(
                [self._bin_busctl, "--user", "status", "rs.wl-gammarelay"],
                capture_output=True,
                timeout=2,
                check=False,
            )
            return out.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def detect_displays(self) -> list[dict[str, Any]]:
        """Run wlr-randr to detect connected display outputs."""
        if not self._bin_randr:
            return []

        try:
            out = subprocess.run(
                [self._bin_randr],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

        displays = []
        # Parse wlr-randr output. Connectors are non-indented lines:
        # DP-1 "Dell Inc. DELL P2419H D4P72V2 (DP-1)"
        for line in out.stdout.splitlines():
            if not line.startswith(" ") and not line.startswith("\t") and line.strip():
                parts = line.split(" ", 1)
                if parts:
                    conn_id = parts[0]
                    displays.append({
                        "connector_id": conn_id,
                        "type": conn_id,
                        "connected": True,
                    })
        return displays

    def set_color(self, connector_id: str, brightness: float, contrast: float) -> bool:
        """Apply brightness and contrast using hyprctl screen shader (on Hyprland) or wl-gammarelay-rs."""
        if not self.is_available():
            return False

        # Ensure values are within reasonable bounds (0.1 to 3.0)
        b_val = max(0.1, min(3.0, brightness))
        c_val = max(0.1, min(3.0, contrast))

        import os
        # 1. Primary path: If we are on Hyprland, generate a custom screen shader dynamically
        if shutil.which("hyprctl") is not None and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            shader_path = "/tmp/nvgui_color.frag"
            shader_content = f"""#version 300 es
precision mediump float;
in vec2 v_texcoord;
layout(location = 0) out vec4 fragColor;
uniform sampler2D tex;

void main() {{
    vec4 color = texture(tex, v_texcoord);
    vec3 rgb = color.rgb;
    // Apply contrast and brightness
    rgb = (rgb - 0.5) * {c_val:.4f} + 0.5 + ({b_val:.4f} - 1.0);
    fragColor = vec4(clamp(rgb, 0.0, 1.0), color.a);
}}
"""
            try:
                with open(shader_path, "w") as f:
                    f.write(shader_content)

                # Reset to empty string if both values are back to defaults
                if abs(b_val - 1.0) < 0.01 and abs(c_val - 1.0) < 0.01:
                    cmd = ["hyprctl", "eval", "hl.config({ decoration = { screen_shader = '' } })"]
                else:
                    cmd = ["hyprctl", "eval", f"hl.config({{ decoration = {{ screen_shader = '{shader_path}' }} }})"]

                subprocess.run(cmd, capture_output=True, timeout=3, check=True)

                if self._settings is not None:
                    self._settings.set(f"color.{connector_id}.brightness", b_val)
                    self._settings.set(f"color.{connector_id}.contrast", c_val)
                return True
            except Exception as exc:
                logger.warning("Hyprland set_color shader failed: %s", exc)

        # 2. Fallback path: wl-gammarelay-rs
        if not self._is_gammarelay_active():
            return False

        try:
            subprocess.run(
                [
                    self._bin_busctl, "--user", "set-property",
                    "rs.wl-gammarelay", "/", "rs.wl.gammarelay",
                    "Brightness", "d", str(b_val)
                ],
                capture_output=True,
                timeout=3,
                check=True,
            )
            
            if self._settings is not None:
                self._settings.set(f"color.{connector_id}.brightness", b_val)
                self._settings.set(f"color.{connector_id}.contrast", c_val)
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            logger.warning("wl-gammarelay set_color failed: %s", exc)
            return False
