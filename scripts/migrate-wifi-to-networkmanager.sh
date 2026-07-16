#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo $0 [interface] [connection]" >&2
  exit 1
fi

INTERFACE=${1:-wlp1s0}
CONNECTION=${2:-AC1750}
INTERFACES_FILE=/etc/network/interfaces
BACKUP="${INTERFACES_FILE}.pre-networkmanager-$(date +%Y%m%d-%H%M%S)"
MIGRATED=false

if ! nmcli -t -f NAME connection show | grep -Fxq "$CONNECTION"; then
  echo "NetworkManager connection '$CONNECTION' does not exist." >&2
  exit 1
fi

rollback() {
  local exit_code=$?
  if [[ $exit_code -ne 0 && $MIGRATED == true ]]; then
    echo "Migration failed; restoring $INTERFACES_FILE from $BACKUP" >&2
    cp -a "$BACKUP" "$INTERFACES_FILE"
    systemctl restart networking.service || true
  fi
  exit "$exit_code"
}
trap rollback EXIT

cp -a "$INTERFACES_FILE" "$BACKUP"
echo "Legacy configuration backed up to $BACKUP"

# Stop the per-interface supplicant and DHCP client started by ifupdown.
ifdown --force "$INTERFACE" || true

# NetworkManager owns non-loopback interfaces after migration.
printf 'auto lo\niface lo inet loopback\n' >"$INTERFACES_FILE"
MIGRATED=true

systemctl restart NetworkManager.service
nmcli connection up "$CONNECTION" ifname "$INTERFACE"

STATE=$(nmcli -g GENERAL.STATE device show "$INTERFACE")
ADDRESS=$(nmcli -g IP4.ADDRESS device show "$INTERFACE" | head -n 1)
if [[ $STATE != 100* || -z $ADDRESS ]]; then
  echo "NetworkManager did not activate $INTERFACE (state=$STATE address=$ADDRESS)" >&2
  exit 1
fi

MIGRATED=false
systemctl disable networking.service
echo "Migration complete: $INTERFACE is active on '$CONNECTION' with $ADDRESS"
