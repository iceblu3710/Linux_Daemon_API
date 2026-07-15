#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo ./scripts/install.sh" >&2
  exit 1
fi

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
INSTALL_DIR=/opt/appliance-admin
CONFIG_DIR=/etc/appliance-admin

getent group appliance-web >/dev/null || groupadd --system appliance-web
id appliance-web >/dev/null 2>&1 || useradd --system --gid appliance-web \
  --home-dir /var/lib/appliance-web --create-home --shell /usr/sbin/nologin appliance-web

install -d -m 0755 "$INSTALL_DIR" "$CONFIG_DIR"
rsync -a --delete --exclude '.venv' --exclude '__pycache__' "$ROOT_DIR/" "$INSTALL_DIR/source/"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install "$INSTALL_DIR/source"

[[ -e "$CONFIG_DIR/daemon.env" ]] || install -m 0640 -o root -g root \
  "$ROOT_DIR/config/daemon.env.example" "$CONFIG_DIR/daemon.env"
[[ -e "$CONFIG_DIR/web.env" ]] || install -m 0640 -o root -g appliance-web \
  "$ROOT_DIR/config/web.env.example" "$CONFIG_DIR/web.env"

install -m 0644 "$ROOT_DIR/systemd/appliance-admin-daemon.service" /etc/systemd/system/
install -m 0644 "$ROOT_DIR/systemd/appliance-admin-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now appliance-admin-daemon.service appliance-admin-web.service

echo "Installed. Edit $CONFIG_DIR/daemon.env to set the exact service allowlist."
