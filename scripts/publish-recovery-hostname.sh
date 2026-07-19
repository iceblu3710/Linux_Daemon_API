#!/usr/bin/env bash
set -euo pipefail

RECOVERY_HOSTNAME=${APPLIANCE_RECOVERY_HOSTNAME:-43a9-9ed7}
IP_BIN=${IP_BIN:-/usr/sbin/ip}
HOSTNAME_BIN=${HOSTNAME_BIN:-/usr/bin/hostname}
AVAHI_PUBLISH_BIN=${AVAHI_PUBLISH_BIN:-/usr/bin/avahi-publish-address}
POLL_SECONDS=${POLL_SECONDS:-1}

if [[ ! $RECOVERY_HOSTNAME =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]]; then
  echo "Invalid APPLIANCE_RECOVERY_HOSTNAME: $RECOVERY_HOSTNAME" >&2
  exit 2
fi

RECOVERY_HOSTNAME=${RECOVERY_HOSTNAME,,}
RECOVERY_FQDN=${RECOVERY_HOSTNAME}.local
declare -A publishers=()

stop_publisher() {
  local address=$1
  local pid=${publishers[$address]:-}
  if [[ -n $pid ]]; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    unset 'publishers[$address]'
  fi
}

stop_all() {
  local address
  for address in "${!publishers[@]}"; do
    stop_publisher "$address"
  done
}

trap stop_all EXIT INT TERM

while true; do
  current_hostname=$($HOSTNAME_BIN 2>/dev/null || true)
  current_hostname=${current_hostname,,}
  current_hostname=${current_hostname%.local}

  # Avahi already owns this name when it is the native system hostname.
  if [[ $current_hostname == "$RECOVERY_HOSTNAME" ]]; then
    stop_all
    sleep "$POLL_SECONDS"
    continue
  fi

  declare -A active_addresses=()
  while read -r _index _interface _family cidr _rest; do
    address=${cidr%/*}
    [[ -n $address ]] && active_addresses[$address]=1
  done < <("$IP_BIN" -o -4 address show up scope global 2>/dev/null || true)

  for address in "${!publishers[@]}"; do
    pid=${publishers[$address]}
    if [[ ! -v 'active_addresses[$address]' ]] || ! kill -0 "$pid" 2>/dev/null; then
      stop_publisher "$address"
    fi
  done

  for address in "${!active_addresses[@]}"; do
    if [[ ! -v 'publishers[$address]' ]]; then
      "$AVAHI_PUBLISH_BIN" --no-reverse "$RECOVERY_FQDN" "$address" &
      publishers[$address]=$!
      echo "Publishing $RECOVERY_FQDN at $address"
    fi
  done

  sleep "$POLL_SECONDS"
done
