#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo $0 [interface] [connection]" >&2
  exit 1
fi

INTERFACE=${1:-wlp1s0}
CONNECTION=${2:-AC1750}
INTERFACES_FILE=/etc/network/interfaces
BACKUP_ROOT=/root/network-backup
BACKUP="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"
MIGRATED=false

if [[ -n ${SSH_CONNECTION:-} && ${APPLIANCE_ADMIN_ALLOW_REMOTE_CUTOVER:-0} != 1 ]]; then
  echo "Refusing a remote Wi-Fi cutover without an explicit override." >&2
  echo "Run this at the local console, use Ethernet fallback, or set" >&2
  echo "APPLIANCE_ADMIN_ALLOW_REMOTE_CUTOVER=1 after accepting the SSH risk." >&2
  exit 1
fi

if ! nmcli -t -f NAME connection show | grep -Fxq "$CONNECTION"; then
  echo "NetworkManager connection '$CONNECTION' does not exist." >&2
  exit 1
fi

rollback() {
  local exit_code=$?
  if [[ $exit_code -ne 0 && $MIGRATED == true ]]; then
    echo "Migration failed; restoring network configuration from $BACKUP" >&2
    cp -a "$BACKUP/interfaces" "$INTERFACES_FILE"
    rm -rf /etc/NetworkManager
    cp -a "$BACKUP/NetworkManager" /etc/NetworkManager
    rm -f /etc/resolv.conf
    cp -a "$BACKUP/resolv.conf" /etc/resolv.conf
    systemctl enable networking.service || true
    systemctl restart networking.service || true
    systemctl restart NetworkManager.service || true
  fi
  exit "$exit_code"
}
trap rollback EXIT

install -d -m 0700 "$BACKUP_ROOT" "$BACKUP"
cp -a "$INTERFACES_FILE" "$BACKUP/interfaces"
cp -a /etc/network/interfaces.d "$BACKUP/interfaces.d"
cp -a /etc/NetworkManager "$BACKUP/NetworkManager"
cp -a /etc/resolv.conf "$BACKUP/resolv.conf"
echo "Network configuration backed up to $BACKUP"
MIGRATED=true

# NetworkManager must exclusively own non-loopback interfaces. Leaving Wi-Fi in
# ifupdown starts a second wpa_supplicant/dhcpcd pair and makes the device
# unavailable to NetworkManager (and therefore to the appliance Wi-Fi API).
printf '# NetworkManager owns all physical interfaces.\n\nauto lo\niface lo inet loopback\n' \
  >"$INTERFACES_FILE"

if grep -RInE 'wlp1s0|eno1|iface.*(dhcp|static)|wpa-' \
  /etc/network/interfaces /etc/network/interfaces.d 2>/dev/null; then
  echo "Physical interface configuration remains under /etc/network; aborting." >&2
  exit 1
fi

rm -f /etc/NetworkManager/conf.d/90-dns-none.conf
install -d -m 0755 /etc/NetworkManager/conf.d
printf '%s\n' \
  '[main]' \
  'plugins=keyfile' \
  'dhcp=internal' \
  'dns=default' \
  'rc-manager=file' \
  '' \
  '[ifupdown]' \
  'managed=false' \
  >/etc/NetworkManager/conf.d/10-appliance-network.conf
chmod 0644 /etc/NetworkManager/conf.d/10-appliance-network.conf
chattr -i /etc/resolv.conf 2>/dev/null || true

systemctl enable NetworkManager.service
systemctl disable networking.service
systemctl disable dhcpcd.service dhcpcd@.service 2>/dev/null || true
systemctl stop networking.service
dhcpcd -k "$INTERFACE" 2>/dev/null || true
systemctl restart NetworkManager.service
nmcli radio wifi on
nmcli device set "$INTERFACE" managed yes
nmcli connection modify "$CONNECTION" \
  connection.autoconnect yes \
  connection.autoconnect-priority 50 \
  ipv4.method auto ipv6.method auto \
  ipv4.route-metric 600 ipv6.route-metric 600
nmcli connection up "$CONNECTION" ifname "$INTERFACE"

STATE=$(nmcli -g GENERAL.STATE device show "$INTERFACE")
ADDRESS=$(nmcli -g IP4.ADDRESS device show "$INTERFACE" | head -n 1)
if [[ $STATE != 100* || -z $ADDRESS ]]; then
  echo "NetworkManager did not activate $INTERFACE (state=$STATE address=$ADDRESS)" >&2
  exit 1
fi

# Every physical Ethernet adapter gets a persistent, high-priority DHCP profile.
# The profile is useful even with no cable present: NetworkManager will activate
# it hungrily whenever carrier appears, including after boot or a cable hot-plug.
while IFS=: read -r ethernet _; do
  [[ -n $ethernet ]] || continue
  profile=""
  while IFS=: read -r candidate_uuid candidate_type; do
    [[ $candidate_type == "802-3-ethernet" ]] || continue
    candidate_interface=$(nmcli -g connection.interface-name connection show "$candidate_uuid")
    if [[ $candidate_interface == "$ethernet" ]]; then
      profile="$candidate_uuid"
      break
    fi
  done < <(nmcli -t -f UUID,TYPE connection show)

  if [[ -z $profile ]]; then
    profile="appliance-ethernet-${ethernet}"
    nmcli connection add type ethernet ifname "$ethernet" con-name "$profile" \
      ipv4.method auto ipv6.method auto
  fi
  nmcli connection modify "$profile" \
    connection.interface-name "$ethernet" \
    connection.autoconnect yes \
    connection.autoconnect-priority 100 \
    connection.autoconnect-retries 0 \
    ipv4.method auto ipv6.method auto \
    ipv4.route-metric 100 ipv6.route-metric 100
  nmcli connection up "$profile" ifname "$ethernet" || true
done < <(nmcli -t -f DEVICE,TYPE device status | awk -F: '$2 == "ethernet" {print $0}')

systemctl disable --now networking.service

# `.local` is the mDNS suffix, not part of the Linux single-label hostname.
# Correct old installations that stored it literally (for example, Avahi would
# otherwise advertise speed-timer.local.local to clients).
current_hostname=$(hostnamectl --static 2>/dev/null || hostname)
if [[ ${current_hostname,,} == *.local ]]; then
  hostnamectl set-hostname "${current_hostname:0:${#current_hostname}-6}"
fi

echo "Resolver configuration:"
cat /etc/resolv.conf
getent hosts chatgpt.com >/dev/null

MIGRATED=false
echo "Migration complete: $INTERFACE is active on '$CONNECTION' with $ADDRESS"
