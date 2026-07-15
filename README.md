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
    managers/system.py    exact service allowlist and reboot
  web/
    app.py                FastAPI routes
    auth.py               replaceable web-auth integration seam
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
| `wifi.connect` | Add and activate an open or WPA-PSK profile |
| `service.status` | Read one allowlisted `.service` unit |
| `service.start` | Start one allowlisted unit |
| `service.stop` | Stop one allowlisted unit |
| `service.restart` | Restart one allowlisted unit |
| `system.reboot` | Reboot the appliance |

## Install on Debian/Ubuntu

Prerequisites:

```bash
sudo apt update
sudo apt install python3 python3-venv network-manager rsync
```

Review `config/daemon.env.example`, especially `APPLIANCE_ADMIN_ALLOWED_SERVICES`, then:

```bash
sudo ./scripts/install.sh
```

Inspect it:

```bash
systemctl status appliance-admin-daemon appliance-admin-web
journalctl -u appliance-admin-daemon -f
ls -l /run/appliance-admin/admin.sock
```

The web bridge listens on `127.0.0.1:8088`. Put your normal authenticated application or same-host reverse proxy in front of it. Do not expose this example bridge directly to a LAN until `web/auth.py` has been replaced or integrated with your real session authentication.

## Authentication model

Authentication is deliberately separated into two decisions:

1. Your web application verifies the human session and admin role.
2. The root daemon verifies that the calling Linux process is `root`, an explicitly configured UID, or a member of `appliance-web`.

The `audit_user` sent through IPC is logged for traceability, but the daemon never trusts it for authorization. A compromised unprivileged web process can invoke the daemon's small allowlist, but cannot expand that allowlist, inject shell arguments, or restart arbitrary units.

The sample `require_admin()` dependency accepts `X-Authenticated-User` and `X-Authenticated-Role: admin` only over loopback. A reverse proxy must strip incoming copies and set them after validating a session. Replacing that dependency with your existing FastAPI session/JWT logic is preferable.

## Example calls

These examples assume a trusted same-host proxy or local test:

```bash
curl -H 'X-Authenticated-User: trevor' -H 'X-Authenticated-Role: admin' \
  http://127.0.0.1:8088/api/admin/network/status

curl -H 'X-Authenticated-User: trevor' -H 'X-Authenticated-Role: admin' \
  http://127.0.0.1:8088/api/admin/wifi/scan

curl -X POST -H 'Content-Type: application/json' \
  -H 'X-Authenticated-User: trevor' -H 'X-Authenticated-Role: admin' \
  -d '{"ssid":"ShopWiFi","password":"replace-me"}' \
  http://127.0.0.1:8088/api/admin/wifi/connect

curl -X POST -H 'X-Authenticated-User: trevor' -H 'X-Authenticated-Role: admin' \
  http://127.0.0.1:8088/api/admin/services/ninja-timer.service/restart
```

## Wi-Fi behaviour

`wifi.scan` returns a recent cache for rapid UI refreshes. A real D-Bus scan is requested no more often than `APPLIANCE_ADMIN_SCAN_MIN_INTERVAL_SECONDS`, and only one scan can run at once. Your UI can refresh frequently without machine-gunning the adapter.

For a polished UI, poll `network.status` at a modest interval such as 2 to 5 seconds, and request `wifi.scan` only while the Wi-Fi chooser is open. A later version can subscribe to NetworkManager property-change signals and push updates over your existing WebSocket.

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
