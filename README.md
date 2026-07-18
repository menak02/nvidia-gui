# nvidia-gui

A Linux-native, open-source alternative to the Windows NVIDIA App — GTK4 + Python
(PyGObject), for Wayland and X11. It bundles the per-game feature gating, DLSS
version swapping, and NVAPI launch injection that Windows users get from NVIDIA's
own app, behind a crash-safe, pure-GTK4 dark theme tuned for compositor stability.

> Not affiliated with NVIDIA Corporation. "NVIDIA", "GeForce", "DLSS", "RTX",
> "Reflex", and "NVAPI" are trademarks of NVIDIA Corporation, used here only as
> nominative references to the hardware and software this tool interoperates
> with. This project is independent and unendorsed.

## Architecture

Hexagonal (ports-and-adapters), so the UI owns no driver or I/O knowledge and
every external effect is mockable in tests:

```
domain          pure models + services — no I/O, no gi, no subprocess
application     port interfaces (ABCs) + UseCases facade
adapters        filesystem / subprocess / Steam / NVIDIA implementations
presentation    GTK4 widgets + views — imports ZERO adapters
composition_root   wires every concrete port together
```

The invariant "presentation imports no adapters" is checked by the test suite.

## Features

**Per-game feature detection** — DLSS-SR, Frame Generation, Reflex, and RT are
gated per game through a confidence-tiered pipeline that never lies:

1. user override (persisted)
2. optional online community DB (`NVIDIA_GUI_FEATURE_DB_URL`; opt-in,
   offline-graceful, default-off so curated results stay deterministic)
3. bundled curated DB (`src/nvidia_gui/assets/nvidia_features.toml`;
   Cyberpunk 2077, appid 1091500, is the pinned example)
4. install-directory DLL probe
5. Steam prefix DLL probe
6. UNKNOWN — detection that has not determined support yet, surfaced as a
   "not yet detected" hint rather than a false "unsupported"

Unsupported features are dimmed with an explicit override chip, so a mis-detection
never silently hides a working feature.

**DLSS version management** — a canonical DLSS page hosts the single Streamline
download (no duplicate buttons), a per-version cache, and a per-game physical DLL
swap with automatic `.nvagogui.orig` backup and clean revert. Detected DLSS kinds:
`dlss`, `dlssd`, `dlssg`, `low_latency_vk`, `deepdvc`.

**Managed launch injection** — once per game (idempotent, user options preserved)
an env file is wired into Steam's `localconfig.vdf` as
`source "<env>" 2>/dev/null; %command%`. The app then edits the env file freely:
`NVAPI_DLSS_*` presets, `PROTON_ENABLE_NVAPI`, `DXVK_ENABLE_NVAPI`, and friends.
State lives under `~/.cache/nvidia-gui/` — not the ephemeral `/run/user/$UID`.

**Settings** — per-game profiles, read-only driver status, and kernel-module
parameter writes through a sudo-elevated install guarded by a polkit action
(`org.mena.nvidia-gui.write-kernel-params`).

**Digital display adjustments** — per-output digital vibrance and digital color (brightness & contrast) controls. Integrated in the Display section with tailored debouncers (200ms/500ms) to prevent screen-shader flickering on Wayland/Hyprland.

**Advanced DLSS/FG overrides** — detailed controls for DLSS-SR letter presets (A through P), DLSS-G Frame Generation presets (A/B), Fixed/Dynamic FG modes, and frame multipliers from 2x up to 6x.

**Motion effects tier** — `full` (default), `minimal`, and `off`, gated by the
`presentation.animations` setting. Both base and effects stylesheets load once;
the tier is chosen by a single CSS class on the root window (no reload, no
flicker). Motion only — no gradients, blur, shadows, or translucent overlays.

**Navigation** — nine pages: Dashboard, Games, Graphics, Display, Drivers, RTX,
Profiles, DLSS, Settings. Original SVG line-icons throughout (no emoji, no
trademarked NVIDIA logo).

## Design language

NVIDIA green (`#76b900`) on near-black (`#000`, `#0b0b`, `#141414`), 2px angular
radii, 1px hairline borders, a single accent. Solid fills only — no gradients,
blur, shadows, or translucent overlays, which stall the NVIDIA compositor on
Wayland. The theme splits into `styles-base.css` (always on, with an in-memory
crash-safe fallback) and `styles-effects.css` (motion-only, silently skipped if
absent).

## Requirements

- Linux, GTK 4 (PyGObject 4.x); tested on the NVIDIA open kernel module
  (610.43.03, RTX 5070)
- Steam for the launch-injection and game-library adapters
- (optional) polkit for kernel-parameter writes; mangohud for the overlay adapter

## Run

```bash
PYTHONPATH=src python3 src/main.py
```

## Test

Headless suite (no pytest dependency):

```bash
python3 tests/test_services.py        # 59 green; Cyberpunk 1091500 is the detection baseline
```

Five-second smoke (expect rc 124, four INFO lines, zero GTK warnings):

```bash
PYTHONPATH=src timeout -k 2 5 python3 src/main.py
```

## Install

```bash
bash install.sh        # prompts for sudo where it touches system files
```

## Status

Fully verified end-to-end, including saving presets, confirming `localconfig.vdf` carries the managed `source` line, launching via Steam, and ensuring the `NVAPI_DLSS_*` keys take effect. The Steam launch option sanitizer correctly strips conflicting manual overrides to ensure the game recognizes the NVIDIA GPU properly (e.g. fixing the generic GPU/grayed-out DLSS bug in Cyberpunk 2077).

## Project layout

```
src/nvidia_gui/
  domain/         models + services (pure)
  application/    ports (ABCs) + use_cases facade
  adapters/       config, steam, dlss, detection, gpu, ipc, kernel-param, profile
  assets/         nvidia_features.toml — bundled curated detection DB
  presentation/   window, views, widgets, theme, icons, download_worker
tests/            test_services.py + game_corpus/ (six game archetypes)
```

## License

MIT — see `LICENSE`.
