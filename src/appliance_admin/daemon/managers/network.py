from __future__ import annotations

import asyncio
import ipaddress
import time
from typing import Any

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus

from appliance_admin.daemon.errors import BusyError, NotFoundError, ValidationError
from appliance_admin.models import (
    InterfaceAddress,
    NetworkInterface,
    NetworkStatus,
    WifiAccessPoint,
    WifiConnectRequest,
)

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
PROPS = "org.freedesktop.DBus.Properties"
NM_IFACE = "org.freedesktop.NetworkManager"
DEVICE = "org.freedesktop.NetworkManager.Device"
WIRELESS = "org.freedesktop.NetworkManager.Device.Wireless"
WIRED = "org.freedesktop.NetworkManager.Device.Wired"
AP = "org.freedesktop.NetworkManager.AccessPoint"
ACTIVE = "org.freedesktop.NetworkManager.Connection.Active"
IP4 = "org.freedesktop.NetworkManager.IP4Config"

DEVICE_TYPE_ETHERNET = 1
DEVICE_TYPE_WIFI = 2
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


def _mac(raw: bytes | bytearray | list[int]) -> str:
    return ":".join(f"{b:02X}" for b in bytes(raw))


def _ipv4_u32(value: int) -> str:
    # NetworkManager's legacy AddressData replacement is preferred; this handles Gateway.
    return str(ipaddress.IPv4Address(value.to_bytes(4, byteorder="little")))


class NetworkManager:
    def __init__(
        self, scan_min_interval: float = 12.0, scan_cache_seconds: float = 8.0
    ):
        self.scan_min_interval = scan_min_interval
        self.scan_cache_seconds = scan_cache_seconds
        self.bus: MessageBus | None = None
        self._scan_lock = asyncio.Lock()
        self._last_scan_request = 0.0
        self._last_scan_result = 0.0
        self._scan_cache: list[dict[str, Any]] = []

    async def start(self) -> None:
        if self.bus is None:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async def close(self) -> None:
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None

    async def _interfaces(self, path: str, names: tuple[str, ...]) -> dict[str, Any]:
        await self.start()
        assert self.bus is not None
        introspection = await self.bus.introspect(NM, path)
        obj = self.bus.get_proxy_object(NM, path, introspection)
        return {name: obj.get_interface(name) for name in names}

    async def _properties(self, path: str, interface: str) -> dict[str, Variant]:
        interfaces = await self._interfaces(path, (PROPS,))
        return await interfaces[PROPS].call_get_all(interface)

    async def _device_paths(self) -> list[str]:
        interfaces = await self._interfaces(NM_PATH, (NM_IFACE,))
        return list(await interfaces[NM_IFACE].call_get_devices())

    async def _wifi_device(self) -> str:
        for path in await self._device_paths():
            props = await self._properties(path, DEVICE)
            if int(props["DeviceType"].value) == DEVICE_TYPE_WIFI:
                return path
        raise NotFoundError("No NetworkManager Wi-Fi device found")

    async def _active_connection_name(self, path: str) -> str | None:
        if path == "/":
            return None
        props = await self._properties(path, ACTIVE)
        return str(props["Id"].value)

    async def _ip4_details(
        self, path: str
    ) -> tuple[list[InterfaceAddress], str | None, list[str]]:
        if path == "/":
            return [], None, []
        props = await self._properties(path, IP4)
        addresses: list[InterfaceAddress] = []
        for item in props.get("AddressData", Variant("aa{sv}", [])).value:
            address_v = item.get("address")
            prefix_v = item.get("prefix")
            if address_v is not None and prefix_v is not None:
                addresses.append(
                    InterfaceAddress(
                        address=str(address_v.value), prefix=int(prefix_v.value)
                    )
                )
        gateway = str(props.get("Gateway", Variant("s", "")).value) or None
        dns_data = props.get("NameserverData", Variant("aa{sv}", [])).value
        dns: list[str] = []
        for item in dns_data:
            address_v = item.get("address")
            if address_v is not None:
                dns.append(str(address_v.value))
        return addresses, gateway, dns

    async def status(self, params: dict) -> dict:
        if params:
            raise ValidationError("network.status does not accept parameters")
        nm_props = await self._properties(NM_PATH, NM_IFACE)
        primary_path = str(nm_props["PrimaryConnection"].value)
        primary_name = await self._active_connection_name(primary_path)
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
            name = str(props["Interface"].value)
            state = DEVICE_STATE_NAMES.get(int(props["State"].value), "unknown")
            active_path = str(props["ActiveConnection"].value)
            active_name = await self._active_connection_name(active_path)
            ip4_path = str(props["Ip4Config"].value)
            addresses, gateway, dns = await self._ip4_details(ip4_path)
            carrier: bool | None = None
            if kind == "ethernet":
                wired_props = await self._properties(path, WIRED)
                carrier = bool(wired_props["Carrier"].value)
            elif kind == "wifi":
                wifi_props = await self._properties(path, WIRELESS)
                active_ap = str(wifi_props["ActiveAccessPoint"].value)
                if active_ap != "/":
                    ap_props = await self._properties(active_ap, AP)
                    current_ssid = _ssid(ap_props["Ssid"].value)
            interfaces.append(
                NetworkInterface(
                    name=name,
                    kind=kind,
                    state=state,
                    carrier=carrier,
                    active_connection=active_name,
                    ipv4=addresses,
                    gateway4=gateway,
                    dns4=dns,
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
        by_ssid: dict[str, WifiAccessPoint] = {}
        for path in paths:
            props = await self._properties(path, AP)
            ssid = _ssid(props["Ssid"].value)
            if not ssid:
                continue
            flags = int(props["Flags"].value)
            wpa = int(props["WpaFlags"].value)
            rsn = int(props["RsnFlags"].value)
            item = WifiAccessPoint(
                ssid=ssid,
                bssid=(
                    _mac(
                        props["HwAddress"].value.encode()
                        if isinstance(props["HwAddress"].value, str)
                        else props["HwAddress"].value
                    )
                    if not isinstance(props["HwAddress"].value, str)
                    else str(props["HwAddress"].value)
                ),
                strength=int(props["Strength"].value),
                frequency_mhz=int(props["Frequency"].value),
                secured=bool(flags or wpa or rsn),
                active=path == active_path,
            )
            old = by_ssid.get(ssid)
            if old is None or item.strength > old.strength:
                by_ssid[ssid] = item
        return [
            x.model_dump()
            for x in sorted(by_ssid.values(), key=lambda x: x.strength, reverse=True)
        ]

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
            now = time.monotonic()
            device = await self._wifi_device()
            if now - self._last_scan_request >= self.scan_min_interval:
                interfaces = await self._interfaces(device, (WIRELESS,))
                try:
                    await interfaces[WIRELESS].call_request_scan({})
                    self._last_scan_request = now
                    await asyncio.sleep(2.0)
                except Exception as exc:
                    # Drivers commonly reject a scan while another is in progress. Return known APs instead.
                    if not self._scan_cache:
                        raise BusyError(f"Wi-Fi scan was rejected: {exc}") from exc
            result = await self._read_access_points(device)
            self._scan_cache = result
            self._last_scan_result = time.monotonic()
            return {"cached": False, "access_points": result}

    async def connect(self, params: dict) -> dict:
        request = WifiConnectRequest.model_validate(params)
        device = await self._wifi_device()
        connection: dict[str, dict[str, Variant]] = {
            "connection": {
                "id": Variant("s", request.ssid),
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
        connection_path, active_path = await nm[
            NM_IFACE
        ].call_add_and_activate_connection(connection, device, "/")
        return {
            "accepted": True,
            "ssid": request.ssid,
            "connection_path": str(connection_path),
            "active_connection_path": str(active_path),
        }
