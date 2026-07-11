# INTEGRATION — contracts the parallel build agents must consume

This is the contract surface the architect produced in Phase 1. Every build
agent (Phase 2) reads its owned files + the files it imports. **Sole-file
ownership**: no two Phase-2 agents edit the same path (see map at the bottom).
Do NOT edit files you don't own — call out a needed contract change in your
return summary instead and the architect (me) reconciles.

App: `org.mena.nvidia-gui`, GTK4/PyGObject, hexagonal. Brand reads **NVIDIA-GUI**.
No `git` in this repo — a revert tarball is at `~/.cache/nvidia-gui/snapshot-pre-effects.tgz`.

---

## New / changed domain + application symbols (already written — use, don't rewrite)

`src/nvidia_gui/domain/models.py`
- `FeatureSource` — str constants: `OVERRIDE="user"`, `ONLINE="community-db"`,
  `BUNDLED="curated"`, `INSTALLDIR="dll-install"`, `PREFIX="dll-prefix"`,
  `UNKNOWN="unknown"`. Precedence highest→lowest in that order.
- `FeatureFlag(supported: bool, source: str = FeatureSource.UNKNOWN)` — frozen.
- `GameCapability(appid, dlss_sr, dlss_fg, reflex, rt [FeatureFlag], notes: str)`
  — frozen; `.is_known()` ⇒ any feature above UNKNOWN. Defaults all-UNKNOWN.

`src/nvidia_gui/application/ports.py`
- `FeatureDetectionPort(ABC).probe(game: Game) -> GameCapability` (abstract).

`src/nvidia_gui/application/use_cases.py` (already wired)
- `UseCases.__init__` gained `detection: FeatureDetectionPort | None = None`
  (optional → existing tests with the functional ports still construct fine).
- `uc.detect_game_features(game) -> GameCapability`  (facade → `self.detection.probe`)
- `uc.feature_override(game, kind) -> bool|None`     (reads `feature.<appid>.<kind>`; `"auto"` ⇒ None)
- `uc.set_feature_override(game, kind, supported)`   (writes bool)
- `uc.clear_feature_override(game, kind)`           (writes `"auto"` sentinel)
- `kind` ∈ `{"dlss_sr","dlss_fg","reflex","rt"}`.

`src/nvidia_gui/composition_root.py` (already wired)
- `UseCases(... detection=SteamFeatureDetector(resolve_install=…, steam_root=…,
  settings=…, bundled_db=…), settings=…)`. Real Steam install dir resolver is
  the same closure the DLSS-swap adapter uses.

`src/nvidia_gui/adapters/feature_detection.py` — **fully wired** by the
  detector-engineer (probe body + helpers; constructor signature kept EXACT
  as the scaffold staged it). Deliberate deviation from the merge rule below:
  the **online community-DB tier is OPT-IN** (`NVIDIA_GUI_FEATURE_DB_URL` env,
  read once at import; absent ⇒ skipped with an "offline" note) so the
  Cyberpunk 1091500 strict-verify stays deterministic (`curated`) and the
  feature is offline-safe by default. Effective default pipeline:
  override → bundled curated DB → install-dir DLL → prefix DLL → UNKNOWN.

`assets/nvidia_features.toml` (at `src/nvidia_gui/assets/`) — seed DB the
  detector reads. Schema `[[games]] appid=… dlss_sr=… dlss_fg=… reflex=… rt=…
  note="…"`. Conservative baseline; online DB + DLL probes refine upward.

## Shared presentation modules (already written — import, don't rewrite)

`src/nvidia_gui/presentation/download_worker.py`
- `StreamlineDownloader.start(uc, bar, label, on_done=None)` (classmethod) — the
  ONE download surface. Replaces the old inline `views._start_download`. dlss-page
  + games-ui import this; games-ui refactors `views._start_download` to delegate.

`src/nvidia_gui/presentation/icons.py`
- `icon(name: str, size: int = 20) -> Gtk.Image` — loads `icons/<name>.svg` or
  falls back to `icons/_placeholder.svg`. `KNOWN = (… 9 nav names + "menu" …)`.
  design-keeper owns the SVG art (`presentation/icons/*.svg`); this loader is final.

`src/nvidia_gui/presentation/views_dlss.py` — **STUB** `build_dlss_view(uc) -> Gtk.Widget`.
  dlss-page-engineer replaces the body; signature fixed.

`src/nvidia_gui/presentation/views_settings.py` — **STUB**
  `build_settings_view(uc, on_anim_changed=None) -> Gtk.Widget`. design-keeper
  replaces the body; signature fixed. on_anim_changed(tier:str) is invoked when
  the user changes the animations tier (window-keeper applies the CSS class).

---

## Animations tier — the contract the whole app honours

Setting key: `presentation.animations` ∈ `{"full", "minimal", "off"}` (default `"full"`).
- design-keeper deletes the STRAY dead `src/styles.css` (repo-root; zero importers —
  packaging bundles `src/nvidia_gui/presentation/*.css`, not this). Split the real
  stylesheet into `styles-base.css` (always) + `styles-effects.css` (effects).
- theme.py: `load_theme()` loads BOTH `styles-base.css` and `styles-effects.css`
  providers ONCE at startup (no per-toggle reload).
- CSS scoping (design-keeper): every effect rule in `styles-effects.css` is gated
  under `window.nvgui-motion-full …`. `off` ⇒ `.nvgui-motion-off *{transition:none!important;
  animation:none!important}`; `minimal` ⇒ ≤60ms transitions, no keyframes/ambient.
- window-keeper: on boot read `uc.setting("presentation.animations","full")` and add a
  root CSS class `nvgui-motion-{tier}` to the window. The Settings page calls
  `on_anim_changed(tier)`, which window-keeper implements as: swap the root CSS class
  + `uc.set_setting("presentation.animations", tier)`. No theme reload — the class
  swap re-scopes the effect rules.
- CSS (verified via context7 `/gnome/gtk`): GTK4 supports `transition:` and
  `animation:`/`@keyframes` natively (NOT a hack). **STILL forbidden**
  (the existing Wayland-compositor-stall DESIGN PRINCIPLE): gradients, shadows,
  translucent overlays, blur. Effects are *motion* only.

## Originality / no-emoji contract (everyone honours)

- **Zero emoji codepoints** anywhere in `src/` (nav, headers, empty states,
  toasts). The emoji nav (`📊🎮🎚🖥📦✨📁`) → `icon(name)` SVGs. Verify:
  `grep -REP "[\x{1F000}-\x{1FAFF}\x{2600}-\x{27BF}]" src/nvidia_gui` ⇒ none.
- Original SVG line-icons only — **no NVIDIA eye logo**, no third-party icon
  font. Strip wordmarks implying endorsement ("GeForce", "NVIDIA App"). Brand
  reads **NVIDIA-GUI**. About page carries one line: "Unaffiliated with NVIDIA
  Corporation. NVIDIA, GeForce, RTX, DLSS are trademarks of NVIDIA Corp."
  Functional feature labels RTX/DLSS/Reflex kept (nominative use).

## Streamline de-duplication — the ONE canonical surface

Three duplicate buttons today: `views.py` Dashboard `:159`, Drivers `:847`,
RTX `:899` (each with its own `_on_download`), all → shared worker. After:
- **the single** Download live on the new **DLSS page** (dlss-page-engineer).
- games-ui-engineer **deletes** the Drivers `:837`-ish and RTX `:884`-ish
  download cards/buttons (keep RTX's "Honest feature parity" card + Drivers'
  "Driver & module" card). The Dashboard quick-action button may stay (it's a
  legit one-glance shortcut, not a 3rd redundant *manager*) OR be removed —
  games-ui-engineer decides; either way `grep "Download Streamline"` ⇒ ≤1 on
  non-DLSS pages. Verify final: `grep -RIn "Download Streamline" src/nvidia_gui`
  ⇒ the DLSS page (+ optionally Dashboard) only.

## Detection merge rule (detector-engineer implements)

For each feature independently, first hit wins by precedence:
1. user override (`self._settings.get(f"feature.<appid>.<kind>")` ⇒ "auto"/None
   = none; bool = FeatureFlag(bool, OVERRIDE)).
2. online community-DB (**OPT-IN** — skipped unless `NVIDIA_GUI_FEATURE_DB_URL`
   is set; default-off keeps Cyberpunk 1091500 ⇒ `curated` deterministic and
   avoids false-positives): HTTP GET a community DLSS-games JSON, cache at
   `~/.cache/nvidia-gui/feature_cache.json` (TTL ~24h). **Never raises** — any
   network/parse failure ⇒ skip this tier (notes may say "offline").
3. bundled `assets/nvidia_features.toml` lookup by appid ⇒ BUNDLED.
4. install-dir DLL probe: `find <abs install dir> -name nvngx_dlss.dll -o
   -name nvapi64.dll` (use `resolve_install(game)`). SR DLL present ⇒
   dlss_sr INSTALLDIR; nvapi64 ⇒ nvapi hint.
5. prefix DLL probe: reuse `adapters/diagnostics._NVAPI_DLLS` +
   compatdata path `steamapps/compatdata/<appid>/pfx/drive_c/windows/system32/`
   (the existing diagnostics probe). PREFIX.
6. else UNKNOWN. `dlss_fg` follows only if SR is supported above UNKNOWN AND
   the source implies a DLSS≥3.5 capable title (online/bundled flag, else
   leave UNKNOWN). `rt` ⇒ dlss.dll present OR bundled/online flags it.

Reuse, don't rebuild: `adapters/diagnostics.py:36 _NVAPI_DLLS` + `:204-219`
prefix probe; the `resolve_install` closure; `adapters/game_library_steam.py`
already yields `Game`. NEVER import a peer adapter directly — only the injected
`resolve_install` closure + `settings` port + `bundled_db` path.

---

## Phase-2 file-ownership map (no overlaps)

| Agent | Owns (edits) | Reads (do not edit) |
|---|---|---|
| detector-engineer | `adapters/feature_detection.py` (+ may extend `use_cases.detect*` bodies if needed, but signatures fixed) | models.py, ports.py, composition_root.py, diagnostics.py, game_library_steam.py, assets/nvidia_features.toml |
| design-keeper | `presentation/styles*.css`, `presentation/icons/*.svg`, `presentation/theme.py`, `presentation/views_settings.py` | icons.py, views_settings.py signature, INTEGRATION, styles.css (current) |
| games-ui-engineer | `presentation/views.py` ONLY | models (GameCapability), use_cases (detect/override), download_worker, icons, diagnostics render pattern |
| dlss-page-engineer | `presentation/views_dlss.py` ONLY | download_worker, views.py (for _card/_scrolled/pill patterns — read, don't edit), use_cases dlss methods |
| window-keeper | `presentation/window.py` ONLY | icons, views_dlss/views_settings factories, theme, use_cases.setting/set_setting |
| robustness-engineer | `tests/test_services.py`, `tests/game_corpus/**` | everything (tests are read-only on src) |

## Verify gate (architect runs after Phase 2)
1. `python3 tests/test_services.py` → 0 fail (Cyberpunk 1091500 regression green).
2. `PYTHONPATH=src timeout -k 2 5 python3 src/main.py` → rc 124/143, ≤4 INFO
   lines (app activate + theme base + theme effects + ipc socket), no Traceback,
   no Gtk-WARNING / Theme parser error.
3. `grep -RE "from nvidia_gui.adapters" src/nvidia_gui/presentation` ⇒ none.
4. `grep -REP "[\x{1F000}-\x{1FAFF}\x{2600}-\x{27BF}]" src/nvidia_gui` ⇒ none.
5. `grep -RIn "Download Streamline" src/nvidia_gui` ⇒ DLSS page (+ optional Dashboard) only.
6. offline detect: no crash, `UNKNOWN`+notes. Per-game gating: supported ⇒ toggle normal; unsupported ⇒ desaturated + override chip persists.
