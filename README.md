# Appliance Admin

A secure local administration pattern for a Linux appliance:

- **FastAPI runs unprivileged** as `appliance-web`.
- **A separate root daemon** owns NetworkManager D-Bus and narrowly allowlisted systemd actions.
- The two communicate using a **Unix-domain socket**, kernel `SO_PEERCRED` identity, group permissions, typed JSON, message limits, and operation timeouts.
- There is deliberately **no arbitrary command**, command string, shell, package installer, file writer, or generic `systemctl` endpoint.
- Wi-Fi scan requests are serialized, rate-limited, and cached to protect flaky adapters and firmware.

NetworkManager exposes configuration and current state through its D-Bus API, and `nmcli` is itself a client of that API. The implementation uses the asyncio-compatible `dbus-next` client rather than spawning `nmcli` for each page refresh.

## Tree

```text
src/appliance_admin/
  daemon/
    main.py               root process entry point
    server.py             Unix socket, SO_PEERCRED, framing, audit log
    managers/network.py   NetworkManager D-Bus, scan cache, connect, status
    managers/system.py    exact service allowlist, reboot, and poweroff
  web/
    app.py                FastAPI routes
    auth.py               signed admin JWT validation
  config.py
  ipc.py
  models.py
systemd/
config/
scripts/install.sh
tests/
```

## Supported privileged actions

| IPC action | Purpose |
|---|---|
| `network.status` | Connectivity, active SSID, Ethernet carrier, addresses, gateway, DNS |
| `wifi.scan` | Serialized and cached AP scan |
| `wifi.connect` | Checkpoint, add, activate, verify, and persist an open or WPA-PSK profile |
| `wifi.disconnect` | Intentionally disconnect the managed Wi-Fi device |
| `network.profiles` | List saved Ethernet/Wi-Fi profiles without secrets |
| `network.profile.activate` | Activate a saved profile by UUID inside a rollback checkpoint |
| `network.profile.delete` | Delete a saved profile by UUID |
| `system.capabilities` | Report live-hostname and immutable recovery-name support |
| `hostname.set` | Persist a validated single-label hostname and publish it live through Avahi |
| `service.status` | Read one allowlisted `.service` unit |
| `service.start` | Start one allowlisted unit |
| `service.stop` | Stop one allowlisted unit |
| `service.restart` | Restart one allowlisted unit |
| `system.reboot` | Reboot the appliance |
| `system.poweroff` | Gracefully power off the appliance |

## Install on Debian/Ubuntu

Prerequisites:

```bash
sudo apt update
sudo apt install python3 python3-venv network-manager avahi-daemon avahi-utils \
  hostname iproute2 rsync
```

Review `config/daemon.env.example`, especially `APPLIANCE_ADMIN_ALLOWED_SERVICES`, then:

```bash
sudo ./scripts/install.sh
```

The installer grants the existing `kiosk` user access to the daemon socket. Set
`APPLIANCE_ADMIN_CLIENT_USER` when the appliance application runs as another user:

```bash
sudo APPLIANCE_ADMIN_CLIENT_USER=ninja-timer ./scripts/install.sh
```

If Wi-Fi is still managed by `/etc/network/interfaces`, migrate an existing
NetworkManager profile before using the Wi-Fi API. This intentionally interrupts
the active Wi-Fi connection, so run it at the local console or with a known-good
Ethernet fallback:

```bash
sudo ./scripts/migrate-wifi-to-networkmanager.sh wlp1s0 AC1750
```

The script backs up the interfaces file and restores it automatically if the
NetworkManager Wi-Fi connection fails to activate. It then gives NetworkManager
exclusive ownership of non-loopback interfaces, configures NetworkManager's
internal DHCP and resolver-file management, disables standalone `dhcpcd` and
legacy `networking.service`, creates an always-autoconnect DHCP profile for every
Ethernet adapter (reusing an existing profile bound to that adapter), and removes
a mistaken `.local` suffix from the static
hostname. Avahi appends `.local` when advertising the single-label hostname.

The installation also enables `appliance-recovery-hostname.service`. It keeps
the compiled-in immutable recovery name `43a9-9ed7.local` published for every
active IPv4 address when the user changes the system hostname. Deployment
environment files cannot override this identity. The publisher follows address
changes and defers to Avahi's native hostname record whenever the system
hostname equals the recovery name.
The `hostname.set` action updates the running Avahi daemon in place, so the new
user-facing `.local` name becomes available without restarting Avahi or the
computer and without interrupting the immutable recovery-name publisher.
It waits for Avahi to confirm the exact requested FQDN and restores both the
Linux and Avahi hostnames if registration fails or a name collision causes
Avahi to select an alternative.

The script refuses an SSH cutover by default. For a deliberately remote cutover
with a tested fallback, explicitly set `APPLIANCE_ADMIN_ALLOW_REMOTE_CUTOVER=1`.

Inspect it:

```bash
systemctl status appliance-admin-daemon appliance-admin-web \
  appliance-recovery-hostname avahi-daemon
journalctl -u appliance-admin-daemon -f
avahi-resolve-host-name -4 43a9-9ed7.local
ls -l /run/appliance-admin/admin.sock
```

The web bridge listens on `127.0.0.1:8088`. Put your authenticated application or same-host reverse proxy in front of it and send a short-lived admin JWT in the `Authorization: Bearer` header.

## Authentication model

Authentication is deliberately separated into two decisions:

1. Your web application verifies the human session and admin role.
2. The root daemon verifies that the calling Linux process is `root`, an explicitly configured UID, or a member of `appliance-web`.

The `audit_user` sent through IPC is logged for traceability, but the daemon never trusts it for authorization. A compromised unprivileged web process can invoke the daemon's small allowlist, but cannot expand that allowlist, inject shell arguments, or restart arbitrary units.

The API validates HS256 JWT signatures, expiry, issuer, audience, subject, and the `admin` role server-side. Set `APPLIANCE_WEB_AUTH_SECRET` to at least 32 random characters and use the same secret in the trusted service that issues short-lived tokens. Identity headers are never trusted.

## Example calls

Set `TOKEN` to a valid short-lived admin JWT issued by your login service:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8088/api/admin/network/status

curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8088/api/admin/wifi/scan

curl -X POST -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"ssid":"ShopWiFi","password":"replace-me"}' \
  http://127.0.0.1:8088/api/admin/wifi/connect

curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8088/api/admin/network/profiles

curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8088/api/admin/services/ninja-timer.service/restart
```

## Wi-Fi behaviour

`wifi.scan` returns a recent cache for rapid UI refreshes. A real D-Bus scan is
requested no more often than `APPLIANCE_ADMIN_SCAN_MIN_INTERVAL_SECONDS`, and
only one scan can run at once. Scan completion follows NetworkManager's
`LastScan` property-change event instead of a fixed sleep.

Connection changes create a 45-second NetworkManager checkpoint by default.
The daemon waits on device/active-connection property events, verifies an IPv4
address, gateway, DNS, and NetworkManager connectivity, then destroys the
checkpoint. Failure rolls back immediately; daemon failure lets NetworkManager's
checkpoint timeout restore the previous state. Configure the interface and
timeouts with `APPLIANCE_ADMIN_WIFI_INTERFACE`,
`APPLIANCE_ADMIN_NETWORK_CHECKPOINT_TIMEOUT_SECONDS`, and
`APPLIANCE_ADMIN_NETWORK_ACTIVATION_TIMEOUT_SECONDS`.

When NetworkManager is absent, the configured Wi-Fi interface is missing, or
the device is unmanaged/unavailable, network operations return the typed
`network_backend_unavailable` error. An empty access-point list therefore means
that a healthy scan found no networks, not that the backend is broken.

## Important production notes

- Keep the service allowlist exact and small.
- Consider disabling `service.stop` for the main application if an accidental stop would strand the UI.
- Require a fresh login or confirmation step before reboot.
- Use CSRF protection for cookie-authenticated POST requests.
- Store the web session key outside the source tree with mode `0600`.
- Keep the API bound to loopback or a Unix socket behind your reverse proxy.
- Test the supplied systemd hardening on the target distribution. If NetworkManager or systemd access is blocked, inspect `journalctl` and loosen only the specific directive responsible.
- Enterprise Wi-Fi, captive portals, static addressing, and hidden-network edge cases need additional typed endpoints rather than generic settings passthrough.

## Development checks

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest
python -m compileall -q src
```
