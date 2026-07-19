from __future__ import annotations

import asyncio
import ipaddress
import time
from contextlib import suppress
from typing import Any
from uuid import UUID, uuid4

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus

from appliance_admin.daemon.errors import (
    BusyError,
    NetworkActivationError,
    NetworkBackendUnavailableError,
    NotFoundError,
    ValidationError,
)
from appliance_admin.models import (
    InterfaceAddress,
    NetworkInterface,
    NetworkProfile,
    NetworkRoute,
    NetworkStatus,
    WifiAccessPoint,
    WifiConnectRequest,
)

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
SETTINGS_PATH = "/org/freedesktop/NetworkManager/Settings"
DBUS = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
PROPS = "org.freedesktop.DBus.Properties"
NM_IFACE = "org.freedesktop.NetworkManager"
DEVICE = "org.freedesktop.NetworkManager.Device"
WIRELESS = "org.freedesktop.NetworkManager.Device.Wireless"
WIRED = "org.freedesktop.NetworkManager.Device.Wired"
AP = "org.freedesktop.NetworkManager.AccessPoint"
ACTIVE = "org.freedesktop.NetworkManager.Connection.Active"
IP4 = "org.freedesktop.NetworkManager.IP4Config"
SETTINGS = "org.freedesktop.NetworkManager.Settings"
SETTINGS_CONNECTION = "org.freedesktop.NetworkManager.Settings.Connection"

DEVICE_TYPE_ETHERNET = 1
DEVICE_TYPE_WIFI = 2
DEVICE_STATE_UNMANAGED = 10
DEVICE_STATE_UNAVAILABLE = 20
DEVICE_STATE_ACTIVATED = 100
DEVICE_STATE_FAILED = 120
ACTIVE_STATE_ACTIVATED = 2
ACTIVE_STATE_DEACTIVATED = 4
CHECKPOINT_DESTROY_ALL = 0x01
CHECKPOINT_DELETE_NEW_CONNECTIONS = 0x02

DEVICE_STATE_NAMES = {
    0: "unknown",
    10: "unmanaged",
    20: "unavailable",
    30: "disconnected",
    40: "prepare",
    50: "config",
    60: "need-auth",
    70: "ip-config",
    80: "ip-check",
    90: "secondaries",
    100: "activated",
    110: "deactivating",
    120: "failed",
}
CONNECTIVITY_NAMES = {0: "unknown", 1: "none", 2: "portal", 3: "limited", 4: "full"}


def _ssid(raw: bytes | bytearray | list[int]) -> str:
    return bytes(raw).decode("utf-8", errors="replace")


def _unwrap(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    return value.value if isinstance(value, Variant) else value


def _validated_uuid(params: dict[str, Any], action: str) -> str:
    if set(params) != {"uuid"}:
        raise ValidationError(f"{action} requires only 'uuid'")
    try:
        return str(UUID(str(params["uuid"])))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("Invalid profile UUID") from exc


class NetworkManager:
    def __init__(
        self,
        scan_min_interval: float = 12.0,
        scan_cache_seconds: float = 8.0,
        *,
        wifi_interface: str = "wlp1s0",
        checkpoint_timeout: int = 45,
        activation_timeout: float = 25.0,
    ):
        self.scan_min_interval = scan_min_interval
        self.scan_cache_seconds = scan_cache_seconds
        self.wifi_interface = wifi_interface
        self.checkpoint_timeout = checkpoint_timeout
        self.activation_timeout = activation_timeout
        self.bus: MessageBus | None = None
        self._scan_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()
        self._last_scan_request = 0.0
        self._last_scan_result = 0.0
        self._scan_cache: list[dict[str, Any]] = []

    async def start(self) -> None:
        if self.bus is not None:
            return
        try:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as exc:
            raise NetworkBackendUnavailableError(
                "Could not connect to the system D-Bus"
            ) from exc

    async def close(self) -> None:
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None

    async def _interfaces(self, path: str, names: tuple[str, ...]) -> dict[str, Any]:
        await self.start()
        assert self.bus is not None
        try:
            introspection = await self.bus.introspect(NM, path)
            obj = self.bus.get_proxy_object(NM, path, introspection)
            return {name: obj.get_interface(name) for name in names}
        except Exception as exc:
            raise NetworkBackendUnavailableError(
                "NetworkManager is unavailable on the system D-Bus"
            ) from exc

    async def _properties(self, path: str, interface: str) -> dict[str, Variant]:
        interfaces = await self._interfaces(path, (PROPS,))
        try:
            return await interfaces[PROPS].call_get_all(interface)
        except Exception as exc:
            raise NetworkBackendUnavailableError(
                f"Could not read NetworkManager object {path}"
            ) from exc

    async def _name_has_owner(self) -> bool:
        await self.start()
        assert self.bus is not None
        try:
            introspection = await self.bus.introspect(DBUS, DBUS_PATH)
            obj = self.bus.get_proxy_object(DBUS, DBUS_PATH, introspection)
            return bool(await obj.get_interface(DBUS).call_name_has_owner(NM))
        except Exception as exc:
            raise NetworkBackendUnavailableError(
                "Could not query the system D-Bus"
            ) from exc

    async def _device_paths(self) -> list[str]:
        interfaces = await self._interfaces(NM_PATH, (NM_IFACE,))
        return list(await interfaces[NM_IFACE].call_get_devices())

    async def _wifi_device(self, *, require_ready: bool = True) -> str:
        if not await self._name_has_owner():
            raise NetworkBackendUnavailableError("NetworkManager service is not active")
        for path in await self._device_paths():
            props = await self._properties(path, DEVICE)
            if (
                int(_unwrap(props.get("DeviceType"), 0)) == DEVICE_TYPE_WIFI
                and str(_unwrap(props.get("Interface"), "")) == self.wifi_interface
            ):
                managed = bool(_unwrap(props.get("Managed"), False))
                state = int(_unwrap(props.get("State"), 0))
                if require_ready and (
                    not managed or state in {DEVICE_STATE_UNMANAGED, DEVICE_STATE_UNAVAILABLE}
                ):
                    raise NetworkBackendUnavailableError(
                        f"NetworkManager does not manage {self.wifi_interface}"
                    )
                return path
        raise NetworkBackendUnavailableError(
            f"NetworkManager Wi-Fi device {self.wifi_interface} does not exist"
        )

    async def readiness(self) -> dict[str, Any]:
        path = await self._wifi_device(require_ready=True)
        props = await self._properties(path, DEVICE)
        return {
            "ready": True,
            "interface": self.wifi_interface,
            "state": DEVICE_STATE_NAMES.get(int(props["State"].value), "unknown"),
        }

    async def _active_connection_name(self, path: str) -> str | None:
        if path == "/":
            return None
        props = await self._properties(path, ACTIVE)
        return str(props["Id"].value)

    async def _ip4_details(
        self, path: str
    ) -> tuple[list[InterfaceAddress], str | None, list[str], list[NetworkRoute]]:
        if path == "/":
            return [], None, [], []
        props = await self._properties(path, IP4)
        addresses: list[InterfaceAddress] = []
        for item in _unwrap(props.get("AddressData"), []):
            address = _unwrap(item.get("address"))
            prefix = _unwrap(item.get("prefix"))
            if address is not None and prefix is not None:
                addresses.append(InterfaceAddress(address=str(address), prefix=int(prefix)))
        gateway = str(_unwrap(props.get("Gateway"), "")) or None
        dns = [
            str(address)
            for item in _unwrap(props.get("NameserverData"), [])
            if (address := _unwrap(item.get("address"))) is not None
        ]
        if not dns:
            for raw in _unwrap(props.get("Nameservers"), []):
                with suppress(ValueError):
                    dns.append(str(ipaddress.IPv4Address(int(raw).to_bytes(4, "little"))))
        routes: list[NetworkRoute] = []
        for item in _unwrap(props.get("RouteData"), []):
            destination = _unwrap(item.get("dest"), "0.0.0.0")
            prefix = _unwrap(item.get("prefix"), 0)
            next_hop = _unwrap(item.get("next-hop"))
            metric = _unwrap(item.get("metric"))
            routes.append(
                NetworkRoute(
                    destination=str(destination),
                    prefix=int(prefix),
                    next_hop=str(next_hop) if next_hop else None,
                    metric=int(metric) if metric is not None else None,
                )
            )
        return addresses, gateway, dns, routes

    async def status(self, params: dict) -> dict:
        if params:
            raise ValidationError("network.status does not accept parameters")
        await self._wifi_device(require_ready=True)
        nm_props = await self._properties(NM_PATH, NM_IFACE)
        primary_name = await self._active_connection_name(
            str(nm_props["PrimaryConnection"].value)
        )
        interfaces: list[NetworkInterface] = []
        current_ssid: str | None = None
        for path in await self._device_paths():
            props = await self._properties(path, DEVICE)
            kind_num = int(props["DeviceType"].value)
            kind = (
                "wifi"
                if kind_num == DEVICE_TYPE_WIFI
                else "ethernet" if kind_num == DEVICE_TYPE_ETHERNET else "other"
            )
            active_name = await self._active_connection_name(
                str(props["ActiveConnection"].value)
            )
            addresses, gateway, dns, routes = await self._ip4_details(
                str(props["Ip4Config"].value)
            )
            carrier: bool | None = None
            if kind == "ethernet":
                carrier = bool((await self._properties(path, WIRED))["Carrier"].value)
            elif kind == "wifi":
                active_ap = str(
                    (await self._properties(path, WIRELESS))["ActiveAccessPoint"].value
                )
                if active_ap != "/":
                    current_ssid = _ssid(
                        (await self._properties(active_ap, AP))["Ssid"].value
                    )
            state_reason = _unwrap(props.get("StateReason"), None)
            if isinstance(state_reason, list | tuple) and len(state_reason) > 1:
                state_reason = state_reason[1]
            interfaces.append(
                NetworkInterface(
                    name=str(props["Interface"].value),
                    kind=kind,
                    state=DEVICE_STATE_NAMES.get(int(props["State"].value), "unknown"),
                    state_reason=int(state_reason) if state_reason is not None else None,
                    managed=bool(_unwrap(props.get("Managed"), False)),
                    carrier=carrier,
                    active_connection=active_name,
                    ipv4=addresses,
                    gateway4=gateway,
                    dns4=dns,
                    routes4=routes,
                )
            )
        return NetworkStatus(
            connectivity=CONNECTIVITY_NAMES.get(
                int(nm_props["Connectivity"].value), "unknown"
            ),
            primary_connection=primary_name,
            wifi_ssid=current_ssid,
            interfaces=interfaces,
        ).model_dump()

    async def _read_access_points(self, device_path: str) -> list[dict[str, Any]]:
        interfaces = await self._interfaces(device_path, (WIRELESS,))
        active_path = str(
            (await self._properties(device_path, WIRELESS))["ActiveAccessPoint"].value
        )
        paths = list(await interfaces[WIRELESS].call_get_all_access_points())
        by_network: dict[tuple[str, bool], WifiAccessPoint] = {}
        for path in paths:
            props = await self._properties(path, AP)
            ssid = _ssid(props["Ssid"].value)
            if not ssid:
                continue
            secured = bool(
                int(props["Flags"].value)
                or int(props["WpaFlags"].value)
                or int(props["RsnFlags"].value)
            )
            item = WifiAccessPoint(
                ssid=ssid,
                bssid=str(props["HwAddress"].value),
                bssids=[str(props["HwAddress"].value)],
                strength=int(props["Strength"].value),
                frequency_mhz=int(props["Frequency"].value),
                secured=secured,
                active=path == active_path,
            )
            key = (ssid, secured)
            old = by_network.get(key)
            if old is None:
                by_network[key] = item
            elif item.strength > old.strength:
                item.bssids = [*old.bssids, item.bssid]
                item.active = item.active or old.active
                by_network[key] = item
            elif item.bssid not in old.bssids:
                old.bssids.append(item.bssid)
                old.active = old.active or item.active
        return [
            item.model_dump()
            for item in sorted(
                by_network.values(), key=lambda item: item.strength, reverse=True
            )
        ]

    async def _request_scan_and_wait(self, device: str) -> None:
        interfaces = await self._interfaces(device, (WIRELESS, PROPS))
        previous = int(
            _unwrap((await self._properties(device, WIRELESS)).get("LastScan"), -1)
        )
        finished = asyncio.Event()

        def changed(interface: str, values: dict[str, Variant], _invalidated: list[str]):
            if interface != WIRELESS or "LastScan" not in values:
                return
            current = int(values["LastScan"].value)
            if current != previous and current >= 0:
                finished.set()

        interfaces[PROPS].on_properties_changed(changed)
        try:
            await interfaces[WIRELESS].call_request_scan({})
            await asyncio.wait_for(
                finished.wait(), timeout=min(8.0, self.activation_timeout)
            )
        except TimeoutError:
            # A completed scan can race with signal subscription on older NM releases.
            current = int(
                _unwrap((await self._properties(device, WIRELESS)).get("LastScan"), -1)
            )
            if current == previous or current < 0:
                raise BusyError("Wi-Fi scan did not complete before timeout")
        finally:
            interfaces[PROPS].off_properties_changed(changed)

    async def scan(self, params: dict) -> dict:
        force = bool(params.get("force", False))
        if set(params) - {"force"}:
            raise ValidationError("wifi.scan accepts only 'force'")
        now = time.monotonic()
        if (
            not force
            and self._scan_cache
            and now - self._last_scan_result < self.scan_cache_seconds
        ):
            return {"cached": True, "access_points": self._scan_cache}
        async with self._scan_lock:
            device = await self._wifi_device(require_ready=True)
            now = time.monotonic()
            if now - self._last_scan_request >= self.scan_min_interval:
                try:
                    await self._request_scan_and_wait(device)
                    self._last_scan_request = now
                except NetworkBackendUnavailableError:
                    raise
                except Exception as exc:
                    if not self._scan_cache:
                        raise BusyError(f"Wi-Fi scan was rejected: {exc}") from exc
            result = await self._read_access_points(device)
            self._scan_cache = result
            self._last_scan_result = time.monotonic()
            return {"cached": False, "access_points": result}

    async def _create_checkpoint(self, device: str) -> str:
        nm = await self._interfaces(NM_PATH, (NM_IFACE,))
        return str(
            await nm[NM_IFACE].call_checkpoint_create(
                [device],
                self.checkpoint_timeout,
                CHECKPOINT_DESTROY_ALL | CHECKPOINT_DELETE_NEW_CONNECTIONS,
            )
        )

    async def _verify_activation(self, device: str, active_path: str) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.activation_timeout
        last_state = 0
        reason: Any = None
        changed_event = asyncio.Event()
        device_interfaces = await self._interfaces(device, (PROPS,))
        active_interfaces = await self._interfaces(active_path, (PROPS,))

        def changed(_interface: str, _values: dict[str, Variant], _invalidated: list[str]):
            changed_event.set()

        device_interfaces[PROPS].on_properties_changed(changed)
        active_interfaces[PROPS].on_properties_changed(changed)
        try:
            while asyncio.get_running_loop().time() < deadline:
                changed_event.clear()
                device_props = await self._properties(device, DEVICE)
                last_state = int(device_props["State"].value)
                reason = _unwrap(device_props.get("StateReason"))
                active_props = await self._properties(active_path, ACTIVE)
                active_state = int(active_props["State"].value)
                if (
                    last_state == DEVICE_STATE_FAILED
                    or active_state == ACTIVE_STATE_DEACTIVATED
                ):
                    raise NetworkActivationError(
                        f"Connection activation failed (device reason={reason})"
                    )
                if (
                    last_state == DEVICE_STATE_ACTIVATED
                    and active_state == ACTIVE_STATE_ACTIVATED
                ):
                    addresses, gateway, dns, _ = await self._ip4_details(
                        str(device_props["Ip4Config"].value)
                    )
                    if addresses and gateway and dns:
                        nm = await self._interfaces(NM_PATH, (NM_IFACE,))
                        connectivity = int(await nm[NM_IFACE].call_check_connectivity())
                        if connectivity in {1, 2, 3}:
                            raise NetworkActivationError(
                                "Connection did not reach full NetworkManager connectivity"
                            )
                        return {
                            "connectivity": CONNECTIVITY_NAMES.get(
                                connectivity, "unknown"
                            ),
                            "addresses": [item.model_dump() for item in addresses],
                            "gateway": gateway,
                            "dns": dns,
                        }
                remaining = deadline - asyncio.get_running_loop().time()
                with suppress(TimeoutError):
                    await asyncio.wait_for(changed_event.wait(), timeout=min(1.0, remaining))
        finally:
            device_interfaces[PROPS].off_properties_changed(changed)
            active_interfaces[PROPS].off_properties_changed(changed)
        raise NetworkActivationError(
            f"Connection did not become usable before timeout (state={last_state}, "
            f"reason={reason})"
        )

    async def _finish_transaction(
        self, checkpoint: str, device: str, active_path: str
    ) -> dict[str, Any]:
        nm = await self._interfaces(NM_PATH, (NM_IFACE,))
        try:
            details = await self._verify_activation(device, active_path)
            await nm[NM_IFACE].call_checkpoint_destroy(checkpoint)
            return details
        except Exception:
            with suppress(Exception):
                await nm[NM_IFACE].call_checkpoint_rollback(checkpoint)
            raise

    async def connect(self, params: dict) -> dict:
        request = WifiConnectRequest.model_validate(params)
        async with self._connection_lock:
            device = await self._wifi_device(require_ready=True)
            checkpoint = await self._create_checkpoint(device)
            connection: dict[str, dict[str, Variant]] = {
                "connection": {
                    "id": Variant("s", f"wifi-{request.ssid}"),
                    "uuid": Variant("s", str(uuid4())),
                    "type": Variant("s", "802-11-wireless"),
                    "autoconnect": Variant("b", True),
                },
                "802-11-wireless": {
                    "ssid": Variant("ay", request.ssid.encode()),
                    "mode": Variant("s", "infrastructure"),
                    "hidden": Variant("b", request.hidden),
                },
                "ipv4": {"method": Variant("s", "auto")},
                "ipv6": {"method": Variant("s", "auto")},
            }
            if request.password:
                connection["802-11-wireless-security"] = {
                    "key-mgmt": Variant("s", "wpa-psk"),
                    "psk": Variant("s", request.password),
                }
            nm = await self._interfaces(NM_PATH, (NM_IFACE,))
            try:
                connection_path, active_path, _ = await nm[
                    NM_IFACE
                ].call_add_and_activate_connection2(
                    connection,
                    device,
                    "/",
                    {"persist": Variant("s", "disk")},
                )
                details = await self._finish_transaction(
                    checkpoint, device, str(active_path)
                )
            except (NetworkActivationError, NetworkBackendUnavailableError):
                with suppress(Exception):
                    await nm[NM_IFACE].call_checkpoint_rollback(checkpoint)
                raise
            except Exception as exc:
                with suppress(Exception):
                    await nm[NM_IFACE].call_checkpoint_rollback(checkpoint)
                raise NetworkActivationError(
                    "NetworkManager rejected the Wi-Fi connection"
                ) from exc
            return {
                "accepted": True,
                "ssid": request.ssid,
                "connection_path": str(connection_path),
                "active_connection_path": str(active_path),
                **details,
            }

    async def disconnect(self, params: dict) -> dict:
        if params:
            raise ValidationError("wifi.disconnect does not accept parameters")
        device = await self._wifi_device(require_ready=True)
        interfaces = await self._interfaces(device, (DEVICE,))
        await interfaces[DEVICE].call_disconnect()
        return {"accepted": True, "interface": self.wifi_interface}

    async def _settings_interface(self) -> Any:
        return (await self._interfaces(SETTINGS_PATH, (SETTINGS,)))[SETTINGS]

    async def profiles(self, params: dict) -> dict:
        if params:
            raise ValidationError("network.profiles does not accept parameters")
        await self._wifi_device(require_ready=True)
        nm_props = await self._properties(NM_PATH, NM_IFACE)
        active_uuids: set[str] = set()
        for active_path in nm_props.get("ActiveConnections", Variant("ao", [])).value:
            active_uuids.add(str((await self._properties(active_path, ACTIVE))["Uuid"].value))
        settings = await self._settings_interface()
        result: list[NetworkProfile] = []
        for path in await settings.call_list_connections():
            interface = (await self._interfaces(path, (SETTINGS_CONNECTION,)))[
                SETTINGS_CONNECTION
            ]
            values = await interface.call_get_settings()
            connection = values.get("connection", {})
            profile_type = str(_unwrap(connection.get("type"), ""))
            if profile_type not in {"802-11-wireless", "802-3-ethernet"}:
                continue
            profile_uuid = str(_unwrap(connection.get("uuid"), ""))
            wireless = values.get("802-11-wireless", {})
            raw_ssid = _unwrap(wireless.get("ssid"))
            result.append(
                NetworkProfile(
                    id=str(_unwrap(connection.get("id"), "")),
                    uuid=profile_uuid,
                    type=profile_type,
                    interface_name=_unwrap(connection.get("interface-name")),
                    ssid=_ssid(raw_ssid) if raw_ssid is not None else None,
                    autoconnect=bool(_unwrap(connection.get("autoconnect"), True)),
                    active=profile_uuid in active_uuids,
                )
            )
        return {"profiles": [item.model_dump() for item in result]}

    async def activate_profile(self, params: dict) -> dict:
        profile_uuid = _validated_uuid(params, "network.profile.activate")
        async with self._connection_lock:
            device = await self._wifi_device(require_ready=True)
            settings = await self._settings_interface()
            try:
                connection_path = str(
                    await settings.call_get_connection_by_uuid(profile_uuid)
                )
            except Exception as exc:
                raise NotFoundError(f"Network profile {profile_uuid} was not found") from exc
            connection_interface = (
                await self._interfaces(connection_path, (SETTINGS_CONNECTION,))
            )[SETTINGS_CONNECTION]
            connection_settings = await connection_interface.call_get_settings()
            profile_type = str(
                _unwrap(connection_settings.get("connection", {}).get("type"), "")
            )
            if profile_type != "802-11-wireless":
                raise ValidationError("Only Wi-Fi profiles can be activated here")
            checkpoint = await self._create_checkpoint(device)
            nm = await self._interfaces(NM_PATH, (NM_IFACE,))
            try:
                active_path = str(
                    await nm[NM_IFACE].call_activate_connection(
                        connection_path, device, "/"
                    )
                )
                details = await self._finish_transaction(checkpoint, device, active_path)
            except (NetworkActivationError, NetworkBackendUnavailableError):
                with suppress(Exception):
                    await nm[NM_IFACE].call_checkpoint_rollback(checkpoint)
                raise
            except Exception as exc:
                with suppress(Exception):
                    await nm[NM_IFACE].call_checkpoint_rollback(checkpoint)
                raise NetworkActivationError(
                    f"NetworkManager rejected profile {profile_uuid}"
                ) from exc
            return {
                "accepted": True,
                "uuid": profile_uuid,
                "active_connection_path": active_path,
                **details,
            }

    async def delete_profile(self, params: dict) -> dict:
        profile_uuid = _validated_uuid(params, "network.profile.delete")
        settings = await self._settings_interface()
        try:
            path = str(await settings.call_get_connection_by_uuid(profile_uuid))
            connection = (await self._interfaces(path, (SETTINGS_CONNECTION,)))[
                SETTINGS_CONNECTION
            ]
            await connection.call_delete()
        except NetworkBackendUnavailableError:
            raise
        except Exception as exc:
            raise NotFoundError(f"Network profile {profile_uuid} was not found") from exc
        return {"deleted": True, "uuid": profile_uuid}
