---
name: nvidia-gui-panel-designer
description: Enforces premium dark-themed hardware monitoring layouts, color-semantic typography, dynamic GTK4 widget hierarchies, and code governance standards for the nvidia-gui project.
---

# NVIDIA-GUI Panel Design & Coding Governance Skill

This skill governs the creation, modification, and auditing of user interface panels and backend adapter code within the `nvidia-gui` project. It ensures visual consistency, high-fidelity motion-tier compatibility, and secure, production-ready coding practices.

---

## 1. Visual Design & Theme Guidelines

All panels must adhere to the premium, dark-themed hardware monitoring theme established in [styles-base.css](file:///home/mena/nvidia-gui/src/nvidia_gui/presentation/styles-base.css):

### Color Palette
- **Primary Background**: Avoid pure black (`#000000`). Use deep graphite/charcoal (`#111111` or `#161616`) to minimize eye strain and screen glare.
- **Card Background**: `#1a1a1a` with a subtle 1px border stroke (`#242424`) to establish physical containment.
- **Text Color Hierarchy**:
  - Primary Labels / Metrics: Pure White (`#ffffff`) or light gray (`#eaeaea`).
  - Subtitles / Metadata: Muted off-white/gray (`#8a8a8a` or `#9a9a9a`).
  - Dynamic Overrides: NVIDIA Green (`#76b900`) for custom configurations, settings active, or successful states.

### Layout Hierarchy
- **Page Headers**: Always use the unified `_page_header` helper from `views.py`. Do not create flat page titles.
  ```python
  self._root.append(_page_header("Title Text", extra_widgets=[action_button]))
  ```
- **Metric Grids**: Group related hardware readings into homogeneous grids using `Gtk.Grid` with `set_row_homogeneous(True)` and `set_column_homogeneous(True)`. Use the `.nvgui-metric-tile` class.
- **Form Controls**: Group toggle switches using `ToggleRow` and inputs in padded cards.

### Dynamic Alert Thresholds (Visual Coding)
Always apply dynamic class-based colors (`.warning` or `.critical`) to alert labels:
- **Temperature**: Warning \(\ge 78^\circ\text{C}\) (`#d27b00`), Critical \(\ge 83^\circ\text{C}\) (`#d20000`).
- **Utilization & VRAM**: High warning \(\ge 90\%\) of maximum limits.

---

## 2. Gtk4 Component Constraints

- **Scale / Sliders**: Never set `set_draw_value(True)` directly on `Gtk.Scale`. Instead, set `set_draw_value(False)` and pair the slider with a fixed-width right-aligned `Gtk.Label` styled with `.nvgui-scale-value`. This prevents layout resizing jitters when slider values change size.
- **Transitions and Motion**: All animations must be CSS-based, decoupled, and gated by the `window` motion classes (`nvgui-motion-full`, `nvgui-motion-minimal`, `nvgui-motion-off`). Never write code-based timer loops to animate properties.
- **Active State Highlights**: All user-overridden settings rows must dynamically swap on/off the `.active-override` class to trigger smooth expansion transitions.

---

## 3. Code Governance & VibeGuard Audit Rules

To maintain codebase safety and reliability during rapid iteration:

### Decoupled Presentation Layer
- Presentation views (e.g., `views.py`, `views_dlss.py`) must act as a dumb front-end layer reading from and writing to `UseCases`.
- Views must **never** import backend adapters (e.g., display/color/modprobe utilities) or directly issue filesystem writes, shell commands, or subprocess execution.

### Boundary & Error Safety
- Always wrap external queries (such as `nvidia-smi` parser calls or Display server detection) in defensive `try/except` wrappers. A failing subsystem must fail gracefully (returning fallback tuples or empty states) and must **never** crash the main GTK application window.
- All sliders or input changes that trigger heavy system commands (like applying vibrance or writing environment configs) must be processed through the `Debouncer` class to prevent terminal spamming or Wayland compositor hangs.

### Secret & Security Gating
- Ensure that no API tokens, GitHub personal access tokens, or private environment credentials are hardcoded into workspace files. Use structured settings or dynamic environment variables.
- Validate all appids and executable files using clean paths. Sanitizer loops (such as in `launch_option_steam.py`) must proactively strip command injection symbols.
