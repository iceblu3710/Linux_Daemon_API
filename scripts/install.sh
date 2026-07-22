#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo ./scripts/install.sh" >&2
  exit 1
fi

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
INSTALL_DIR=/opt/appliance-admin
CONFIG_DIR=/etc/appliance-admin
CLIENT_USER=${APPLIANCE_ADMIN_CLIENT_USER:-kiosk}

for required_executable in \
  /usr/bin/hostname /usr/bin/hostnamectl /usr/bin/avahi-set-host-name /usr/bin/busctl; do
  if [[ ! -x $required_executable ]]; then
    echo "Missing required executable: $required_executable" >&2
    echo "Install the systemd, hostname, avahi-daemon, and avahi-utils packages." >&2
    exit 1
  fi
done

getent group appliance-web >/dev/null || groupadd --system appliance-web
id appliance-web >/dev/null 2>&1 || useradd --system --gid appliance-web \
  --home-dir /var/lib/appliance-web --create-home --shell /usr/sbin/nologin appliance-web
if id "$CLIENT_USER" >/dev/null 2>&1; then
  usermod -a -G appliance-web "$CLIENT_USER"
else
  echo "Warning: client user '$CLIENT_USER' does not exist; socket access was not granted." >&2
fi

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
install -m 0755 "$ROOT_DIR/scripts/publish-recovery-hostname.sh" \
  /usr/local/libexec/appliance-publish-recovery-hostname
install -m 0644 "$ROOT_DIR/systemd/appliance-recovery-hostname.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now appliance-admin-daemon.service appliance-admin-web.service \
  appliance-recovery-hostname.service

echo "Installed. Edit $CONFIG_DIR/daemon.env to set the exact service allowlist."
echo "Restart services for new group membership to take effect for '$CLIENT_USER'."
