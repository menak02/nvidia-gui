# NVreg Registry Dwords — reference

This documents the NVIDIA kernel-module registry keys that **nvidia-gui** can
write, what they do, and how they take effect. It covers the curated keys we
expose in the UI, the always-managed lines, and how to add your own.

## How these settings take effect

On Linux there is no "NVIDIA Control Panel registry" like on Windows. Instead,
NVIDIA's open and proprietary GPU kernel modules accept a string of
`Key=Value` pairs through the **`NVreg_RegistryDwords`** module option. The
module reads that string **once at load time**, so any change only takes effect
after the module is reloaded — which on a running desktop practically means
**the next reboot** (or reloading the display stack).

nvidia-gui writes the values to `/etc/modprobe.d/nvidia-gui.conf` through a
polkit / `pkexec` privileged helper (action
`org.mena.nvidia-gui.write-kernel-params`) and they apply on the next boot. The
app never hot-patches a live module.

## The always-managed lines

These are written by nvidia-gui regardless of the Registry Dwords field:

| Line | Effect |
|---|---|
| `options nvidia_drm modeset=1` | Enables DRM/KMS on the `nvidia-drm` device. **Required** for Wayland sessions, for PRIME / hybrid output, and for VRR (G-SYNC / FreeSync) over DRM. nvidia-gui always keeps this on. |
| `options nvidia NVreg_EnableResizableBar=0|1` | Toggled by the "Resizable BAR" switch on the Graphics page (see below). |

## Curated registry keys (the Registry Dwords field)

### `PerfLevelSrc`
- **Shape:** a hex dword, e.g. `0x3322` (validated as `^0x[0-9a-fA-F]{1,8}$`).
- **General effect:** selects which **performance-level source** the PowerMizer
  governor consults when deciding clock speeds — i.e. which input "wins" when
  deciding to ramp clocks up or down.
- **Caution:** the meaning of individual hex digits/nibbles is GPU- and
  firmware-defined and varies by GPU family. nvidia-gui validates only the
  *shape* (a hex dword), not its semantics — paste a value you have verified
  for your specific GPU and driver branch.

### `PowerMizerEnable`
- **Shape:** `0` or `1`.
- **General effect:** `1` enables the adaptive **PowerMizer** clock governor
  (dynamic clocking by load); `0` disables it (clocks fixed per the configured
  perf level). Paired with `PerfLevelSrc`, the two let you coax the card toward
  a preferred clock state.

### `NVreg_EnableResizableBar` (separate toggle, not a dword entry)
- **Shape:** `0` or `1`.
- **Effect:** `1` requests PCI **Resizable BAR**, opening a 1 GiB+ aperture so
  the CPU can address the GPU's full VRAM in one mapping instead of paging
  through a 256 MiB window. Improves PCIe-bandwidth-bound games on supporting
  systems.
- **Requirements:** motherboard BIOS with ReBAR / Above-4G-decoding enabled
  (usually with CSM off), a GPU/driver that advertises the capability, and
  kernel support. Applied at module load (next boot). nvidia-gui exposes this
  as the "Resizable BAR" switch on the Graphics page.

## Adding your own keys (power users)

The Registry Dwords box accepts any `Key=Value;` chunks, separated by `;` or
`,`. Un-curated keys are **loosely shape-checked** only:

- key name: `^[A-Za-z][A-Za-z0-9_]*$` (PascalCase-ish identifier)
- value: a hex dword `0xNNNN`, a small int `0–9999`, or a single bit `0/1`

So if you know an RmRegistry key for your GPU (from another tuning tool, a
forum thread you trust, etc.), you can paste it here without needing nvidia-gui
to curate it. Anything that fails shape validation is reported in the status
line and is **not** written.

### `NVreg_RegistryDwordsPerDevice`
- Optional companion to `NVreg_RegistryDwords`. A device-scoped override string
  (`<pci-bdf>:<Key>=<Value>;`, e.g. `02:00.0:PerfLevelSrc=0x3322;`) so a key can
  target one GPU in a multi-GPU box while the global field applies to all.

## A note on honesty

nvidia-gui describes these keys at the level of their **general, well-known
effect**. NVIDIA's open GPU kernel modules expose the `NVreg_RegistryDwords`
plumbing — the parameter that forwards the string into the driver's internal
registry table — but the **bit-level semantics** of each key are defined by the
closed / firmware side and are deliberately **not** enumerated here: they are
GPU-family specific and subject to change. Treat any external "bit map" table
you find with scepticism unless it matches your exact GPU and driver branch,
and verify before writing.

## Sources / further reading
- NVIDIA open GPU kernel modules — module parameters (`NVreg_*`) and the
  `RegistryDwords` / `RegistryDwordsPerDevice` options.
- The app's own validators live in
  `src/nvidia_gui/domain/services.py` (`validate_registry_dwords`,
  `compose_registry_dwords`); the modprobe.d rendering in
  `src/nvidia_gui/adapters/kernel_param_modprobe.py`.

---

*Managed by nvidia-gui. These settings apply at the next reboot.*
