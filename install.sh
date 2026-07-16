#!/usr/bin/env bash
#
# nvidia-gui installer.
#
# This installer is PRIVILEGE-AWARE: it self-elevates with sudo so it can write
# the kernel-module configuration (decision #1 — "elevate the installation with
# sudo and it can affect the kernel during it"). The privileged bits that need
# root are installed as root; the user-scoped bits (pip --user, ~/.local/bin,
# shell hooks) are re-run as the ORIGINAL user (SUDO_USER), never as root.
#
# SECURITY: this script embeds NO credentials. It never puts a password on the
# command line or in a file. sudo/polkit prompt interactively at runtime for
# the elevated steps, and the polkit action's own-password prompt (auth_self_keep)
# is used by the app at runtime via the dedicated helper.
#
# Idempotent: safe to re-run. Existing modprobe config / rc edits are preserved,
# never clobbered or duplicated.

set -euo pipefail

APP="nvidia-gui"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- install roots (overridable) -------------------------------------------
PREFIX="${INSTALL_PREFIX:-/usr}"
BINDIR="${INSTALL_BINDIR:-${PREFIX}/bin}"
LIBEXEC="${INSTALL_LIBEXEC:-${PREFIX}/libexec}"
APPS_DIR="${INSTALL_APPS_DIR:-${PREFIX}/share/applications}"
ICONS_DIR="${PREFIX}/share/icons/hicolor/scalable/apps"
# AppStream metainfo (catalog metadata — summary, release notes, OARS). Newest
# AppStream (1.19+) prefers metainfo under /usr/share/metainfo; older
# appstream-glib installs read /usr/share/appdata. Write to metainfo (the
# current canonical path) — install.sh targets a fixed distro, not both.
METAINFO_DIR="${PREFIX}/share/metainfo"
POLKIT_ACTIONS="${PREFIX}/share/polkit-1/actions"
POLKIT_RULES="/etc/polkit-1/rules.d"
MODPROBED="/etc/modprobe.d"

# --- helpers ---------------------------------------------------------------
c_ok()   { printf '\033[32m✔\033[0m %s\n' "$*"; }
c_step() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }

real_user() { echo "${SUDO_USER:-${USER:-$(id -un)}}"; }
# Resolve the original user's home dir robustly (env HOME under sudo is
# unreliable; getent always returns the passwd db value).
user_home() { getent passwd "$(real_user)" | cut -d: -f6; }
user_uid()  { id -u "$(real_user)"; }
user_gid()  { id -g "$(real_user)"; }

# Run a command as the ORIGINAL user (even when this script is root).
as_user() {
    if [ "$(id -u)" -eq 0 ]; then
        sudo -u "$(real_user)" -- "$@"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# Root-only portion
# ---------------------------------------------------------------------------
install_system_bits() {
    c_step "Installing privileged system files (as root)"

    install -d -m 0755 "$LIBEXEC" "$POLKIT_ACTIONS" "$POLKIT_RULES" \
                     "$APPS_DIR" "$ICONS_DIR" "$METAINFO_DIR" "$MODPROBED"

    # dedicated polkit action + rules so the app's runtime elevation names us
    install -m 0644 "$HERE/data/polkit/org.mena.${APP}.policy" \
                    "$POLKIT_ACTIONS/org.mena.${APP}.policy"
    install -m 0644 "$HERE/data/polkit/49-${APP}.rules" \
                    "$POLKIT_RULES/49-${APP}.rules"
    c_ok "polkit action + rules → ${POLKIT_ACTIONS}, ${POLKIT_RULES}"

    # privileged helper (invoked by pkexec; resolves to our polkit action)
    install -m 0755 "$HERE/scripts/${APP}-apply-modprobe" \
                    "$LIBEXEC/${APP}-apply-modprobe"
    c_ok "privileged helper → ${LIBEXEC}/${APP}-apply-modprobe"

    # desktop entry + icon (system-wide launcher)
    install -m 0644 "$HERE/data/applications/${APP}.desktop" "$APPS_DIR/${APP}.desktop"
    install -m 0644 "$HERE/data/icons/${APP}.svg" "$ICONS_DIR/${APP}.svg"
    c_ok "desktop entry + icon → ${APPS_DIR}, ${ICONS_DIR}"

    # AppStream metainfo (catalog metadata — summary, release notes, OARS).
    # Validator-clean (appstreamcli) at author time; distro caches pick it up.
    install -m 0644 "$HERE/data/metainfo/org.mena.${APP}.metainfo.xml" \
                    "$METAINFO_DIR/org.mena.${APP}.metainfo.xml"
    c_ok "metainfo → ${METAINFO_DIR}"

    # kernel-module skeleton — only if NOT already present (never clobber tuning)
    if [ -f "${MODPROBED}/${APP}.conf" ]; then
        c_ok "existing ${MODPROBED}/${APP}.conf preserved (the app will update it)"
    else
        cat > "${MODPROBED}/${APP}.conf" <<'CONF'
# Managed by nvidia-gui — applied at next boot. Edit via the NVIDIA-GUI app.
options nvidia_drm modeset=1
options nvidia NVreg_EnableResizableBar=1
CONF
        chmod 0644 "${MODPROBED}/${APP}.conf"
        c_ok "wrote kernel-module skeleton → ${MODPROBED}/${APP}.conf (reboot to apply)"
    fi

    # refresh caches so the new desktop/polkit/icon show up immediately
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q "$APPS_DIR" 2>/dev/null || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f -t "$(dirname "$ICONS_DIR")/.." 2>/dev/null || true
    fi
    # Refresh the AppStream cache so software centers surface the app at once.
    # `appstreamcli refresh-cache --verbose` exists on AppStream ≥ 0.12;
    # `update-appstream-cache` is the appstream-glib-era equivalent. Both are
    # best-effort (the distro builder also picks metainfo up at package time).
    if command -v appstreamcli >/dev/null 2>&1; then
        appstreamcli refresh-cache --verbose >/dev/null 2>&1 || true
    elif command -v update-appstream-cache >/dev/null 2>&1; then
        update-appstream-cache >/dev/null 2>&1 || true
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl reload polkit 2>/dev/null || \
            systemctl reload polkit.service 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# User-only portion
# ---------------------------------------------------------------------------
install_user_bits() {
    c_step "Installing user-facing bits (as $(real_user))"

    local HOME_DIR BIN_DIR
    HOME_DIR="$(user_home)"
    BIN_DIR="${HOME_DIR}/.local/bin"
    as_user install -d -m 0755 "${HOME_DIR}/.local/bin"

    # 1) prefer an editable --user install (keeps the repo checkout live).
    #    On PEP-668 / "externally-managed" distros (Arch!) plain pip refuses,
    #    so add --break-system-packages (a user-scope install is still safe).
    local pip_log="/tmp/${APP}-install-pip.log"
    if as_user python3 -m pip install --user --break-system-packages -e "$HERE" >"$pip_log" 2>&1; then
        c_ok "pip --user editable install → 'nvidia-gui' on PATH"
    else
        echo "  pip install could not run ($(tail -n1 "$pip_log" 2>/dev/null)); using a PATH wrapper instead"
        # fallback: a thin wrapper in ~/.local/bin so 'nvidia-gui' still runs.
        as_user tee "${BIN_DIR}/${APP}" >/dev/null <<WRAP
#!/bin/sh
# Managed by nvidia-gui install.sh. Falls back when a pip --user editable
# install is unavailable (e.g. externally-managed Python on a distro that
# forbids system pip writes). Safe to delete once 'nvidia-gui' is on PATH.
exec python3 "${HERE}/src/main.py" "\$@"
WRAP
        as_user chmod 0755 "${BIN_DIR}/${APP}"
        c_ok "PATH wrapper → ${BIN_DIR}/${APP}"
    fi

    # 2) XDG base dirs the app expects (it creates these lazily too — harmless)
    as_user mkdir -p \
        "${HOME_DIR}/.local/share/${APP}" \
        "${HOME_DIR}/.config/${APP}" \
        "${HOME_DIR}/.cache/${APP}"

    install_shell_hooks "${HOME_DIR}"

    install_summary
}

# ---------------------------------------------------------------------------
# Shell convenience hooks — idempotent (guarded against duplicate lines/files)
# Designed for the user's fish-first setup; bash/zsh covered too.
# ---------------------------------------------------------------------------
install_shell_hooks() {
    local HOME_DIR="$1"

    # fish: a single sourced conf.d file (overwrite = idempotent)
    local fish_conf="${HOME_DIR}/.config/fish/conf.d/${APP}.fish"
    as_user sh -c '
        d="$(dirname "$1")"; mkdir -p "$d";
        cat > "$1" <<'"'"'FISH'"'"'
# nvidia-gui convenience hook (managed by install.sh).
# The real command is on PATH after install; "nvgui" is just a short alias.
alias nvgui nvidia-gui
FISH
    ' _ "$fish_conf"
    c_ok "fish hook → ${fish_conf}"

    # bash / zsh: guarded append so re-installs never duplicate the block
    for rc in "${HOME_DIR}/.bashrc" "${HOME_DIR}/.zshrc"; do
        [ -f "$rc" ] || continue
        if ! grep -q 'nvidia-gui convenience alias' "$rc" 2>/dev/null; then
            printf '\n# nvidia-gui convenience alias\nalias nvgui=nvidia-gui\n' >> "$rc"
        fi
    done
    c_ok "bash/zsh alias 'nvgui' added (if you use them)"
}

install_summary() {
    local HOME_DIR; HOME_DIR="$(user_home)"
    c_step "Done"
    cat <<MSG
  $(tput bold)nvidia-gui$(tput sgr0) installed.

  Run it from the menu, or:
      nvidia-gui            # the app
      nvgui                 # short alias (fish/bash/zsh)
      ${APP}-apply-modprobe  # the privileged helper (via pkexec)

  Kernel-module settings live in $(tput bold)${MODPROBED}/${APP}.conf$(tput sgr0)
  and apply at the $(tput bold)next reboot$(tput sgr0). The app edits them through the
  polkit action $(tput bold)org.mena.${APP}.write-kernel-params$(tput sgr0).

  Paths:
      app data     ${HOME_DIR}/.local/share/${APP}
      per-game env  ${HOME_DIR}/.cache/${APP}/profiles · <…>/env
      helper       ${LIBEXEC}/${APP}-apply-modprobe
      polkit       ${POLKIT_ACTIONS}/org.mena.${APP}.policy

MSG
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
main() {
    if [ ! -f "$HERE/pyproject.toml" ]; then
        echo "install.sh must be run from the nvidia-gui repository root" >&2
        exit 2
    fi

    if [ "$(id -u)" -ne 0 ]; then
        cat <<ELEV
This installer writes to ${MODPROBED}/, ${POLKIT_RULES}/, and under ${PREFIX}/,
so it needs root for those steps. Re-running itself with sudo (user-scoped
steps are still done as $(real_user)). You may be prompted for your password.

ELEV
        exec sudo -E -p "[sudo] install nvidia-gui system bits: " "$0" "$@"
    fi

    install_system_bits
    install_user_bits
}

main "$@"
