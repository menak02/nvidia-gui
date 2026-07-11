#!/usr/bin/env python3
"""Backward-compat shim — the real config store now lives in the package.

Historical importers (``from config_manager import get_config_manager``) keep
working. New code should import from :mod:`nvidia_gui.adapters.config_toml`.
"""
from __future__ import annotations

from nvidia_gui.adapters.config_toml import (  # noqa: F401
    ConfigManager,
    XDGPaths,
    get_config_manager,
)

SOCKET_PATH = get_config_manager().paths.socket
