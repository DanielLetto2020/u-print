#!/usr/bin/env bash
# Simple "install on this Ubuntu 24.04 system without packaging" recipe.
# It installs system dependencies via apt, drops the Python package into
# /opt/photoprint, and links the desktop integration files into /usr/local.
# Not a real .deb — for that, use `dh-virtualenv` or `flatpak-builder`.
set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
    echo "Re-running with sudo…"
    exec sudo "$0" "$@"
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_PREFIX="${INSTALL_PREFIX:-/opt/photoprint}"

apt update
apt install -y python3-gi python3-cups gir1.2-gtk-4.0 gir1.2-adw-1 \
               libheif1 libheif-plugin-libde265 \
               python3-pip python3-venv

python3 -m venv --system-site-packages "$INSTALL_PREFIX"
"$INSTALL_PREFIX/bin/pip" install --upgrade pip
"$INSTALL_PREFIX/bin/pip" install "$REPO_ROOT"

# Desktop integration
install -Dm644 "$REPO_ROOT/data/desktop/io.github.photoprint.PhotoPrint.desktop" \
               /usr/local/share/applications/io.github.photoprint.PhotoPrint.desktop
install -Dm644 "$REPO_ROOT/data/icons/io.github.photoprint.PhotoPrint.svg" \
               /usr/local/share/icons/hicolor/scalable/apps/io.github.photoprint.PhotoPrint.svg

python3 "$REPO_ROOT/po/build_mo.py" >/dev/null
for L in "$REPO_ROOT"/po/*/LC_MESSAGES/photoprint.mo; do
    locale=$(basename "$(dirname "$(dirname "$L")")")
    install -Dm644 "$L" "/usr/local/share/locale/$locale/LC_MESSAGES/photoprint.mo"
done

ln -sf "$INSTALL_PREFIX/bin/photoprint" /usr/local/bin/photoprint
echo "Installed. Launch with: photoprint  (or from the Activities menu)"
