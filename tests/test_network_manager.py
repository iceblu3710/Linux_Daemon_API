from __future__ import annotations

import pytest
from dbus_next import Variant

from appliance_admin.daemon.errors import (
    NetworkActivationError,
    NetworkBackendUnavailableError,
)
from appliance_admin.daemon.managers.network import (
    CHECKPOINT_DELETE_NEW_CONNECTIONS,
    CHECKPOINT_DESTROY_ALL,
    DEVICE,
    NM_IFACE,
    NetworkManager,
)


class FakeNM:
    def __init__(self):
        self.created = None
        self.rolled_back = []
        self.destroyed = []

    async def call_checkpoint_create(self, devices, timeout, flags):
        self.created = (devices, timeout, flags)
        return "/checkpoint/1"

    async def call_checkpoint_rollback(self, path):
        self.rolled_back.append(path)
        return {}

    async def call_checkpoint_destroy(self, path):
        self.destroyed.append(path)


class FakeNetworkManager(NetworkManager):
    def __init__(self, properties):
        super().__init__(wifi_interface="wlp1s0")
        self.fake_properties = properties

    async def _name_has_owner(self):
        return True

    async def _device_paths(self):
        return ["/device/wifi"]

    async def _properties(self, path, interface):
        assert interface == DEVICE
        return self.fake_properties


@pytest.mark.asyncio
async def test_wifi_device_rejects_unmanaged_interface():
    manager = FakeNetworkManager(
        {
            "DeviceType": Variant("u", 2),
            "Interface": Variant("s", "wlp1s0"),
            "Managed": Variant("b", False),
            "State": Variant("u", 10),
        }
    )

    with pytest.raises(
        NetworkBackendUnavailableError,
        match="NetworkManager does not manage wlp1s0",
    ):
        await manager._wifi_device()


@pytest.mark.asyncio
async def test_checkpoint_has_rollback_protection_flags(monkeypatch):
    manager = NetworkManager(checkpoint_timeout=45)
    fake_nm = FakeNM()

    async def fake_interfaces(path, names):
        return {NM_IFACE: fake_nm}

    monkeypatch.setattr(manager, "_interfaces", fake_interfaces)
    checkpoint = await manager._create_checkpoint("/device/wifi")

    assert checkpoint == "/checkpoint/1"
    assert fake_nm.created == (
        ["/device/wifi"],
        45,
        CHECKPOINT_DESTROY_ALL | CHECKPOINT_DELETE_NEW_CONNECTIONS,
    )


@pytest.mark.asyncio
async def test_failed_activation_rolls_back_checkpoint(monkeypatch):
    manager = NetworkManager()
    fake_nm = FakeNM()

    async def fake_interfaces(path, names):
        return {NM_IFACE: fake_nm}

    async def fail_verification(device, active_path):
        raise NetworkActivationError("bad password")

    monkeypatch.setattr(manager, "_interfaces", fake_interfaces)
    monkeypatch.setattr(manager, "_verify_activation", fail_verification)

    with pytest.raises(NetworkActivationError, match="bad password"):
        await manager._finish_transaction(
            "/checkpoint/1", "/device/wifi", "/active/1"
        )

    assert fake_nm.rolled_back == ["/checkpoint/1"]
    assert fake_nm.destroyed == []
