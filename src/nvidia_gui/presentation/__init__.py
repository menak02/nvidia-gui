"""Presentation layer — GTK4 UI. NVIDIA-NV-App-styled, crash-safe on Wayland.

Crash-safety contract (the thing this whole project exists to get right):

* **No compositor-heavy effects.** NVIDIA's own design language is exactly this
  anyway — solid fills, 2px angular radii, hairline 1px borders, a single
  green accent. There is no ``gradient``, ``backdrop-filter``, ``box-shadow``
  or translucent overlay on any root surface. On a NVIDIA + Wayland box these
  trigger compositor/fragment-shader stalls; omitting them makes the app stable
  by construction, not by luck.
* **No UI thread blocking.** nvidia-smi polling runs on a worker thread;
  results are marshalled back via ``GLib.idle_add``. The graph redraws with a
  cheap Cairo solid stroke.
* **The IPC server is on its own thread** and never touches GTK.
"""
