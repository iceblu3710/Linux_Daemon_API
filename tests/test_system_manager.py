import pytest
from pydantic import ValidationError as PydanticValidationError

from appliance_admin.daemon.errors import NotFoundError, ValidationError
from appliance_admin.daemon.managers.system import SystemManager


def test_service_allowlist_accepts_exact_unit():
    manager = SystemManager(["ninja-timer.service"])
    assert manager._validate_service("ninja-timer.service") == "ninja-timer.service"


def test_service_allowlist_rejects_other_unit():
    manager = SystemManager(["ninja-timer.service"])
    with pytest.raises(ValidationError):
        manager._validate_service("ssh.service")


def test_service_name_rejects_argument_injection():
    manager = SystemManager(["ninja-timer.service;reboot.service"])
    with pytest.raises(ValidationError):
        manager._validate_service("ninja-timer.service;reboot.service")


@pytest.mark.asyncio
async def test_hostname_rejects_argument_injection():
    manager = SystemManager([])
    with pytest.raises(PydanticValidationError):
        await manager.hostname_set({"hostname": "timer; reboot"})


@pytest.mark.asyncio
async def test_hostname_updates_linux_and_avahi_without_restart(monkeypatch):
    manager = SystemManager([])
    calls = []

    async def fake_run_command(*args, timeout=20.0):
        calls.append((args, timeout))
        return ""

    monkeypatch.setattr(manager, "_run_command", fake_run_command)

    result = await manager.hostname_set({"hostname": "speed-timer"})

    assert result == {
        "hostname": "speed-timer",
        "local_hostname": "speed-timer.local",
        "mdns_updated": True,
        "accepted": True,
    }
    assert calls == [
        (("/usr/bin/hostnamectl", "set-hostname", "speed-timer"), 10.0),
        (("/usr/bin/avahi-set-host-name", "speed-timer"), 10.0),
    ]


@pytest.mark.asyncio
async def test_hostname_reports_avahi_failure_without_restarting_services(monkeypatch):
    manager = SystemManager([])
    calls = []

    async def fake_run_command(*args, timeout=20.0):
        calls.append(args)
        if args[0] == "/usr/bin/avahi-set-host-name":
            raise NotFoundError("Avahi is unavailable")
        return ""

    monkeypatch.setattr(manager, "_run_command", fake_run_command)

    with pytest.raises(ValidationError, match="mDNS hostname could not be updated"):
        await manager.hostname_set({"hostname": "speed-timer"})

    assert calls == [
        ("/usr/bin/hostnamectl", "set-hostname", "speed-timer"),
        ("/usr/bin/avahi-set-host-name", "speed-timer"),
    ]


@pytest.mark.asyncio
async def test_reboot_rejects_parameters(monkeypatch):
    manager = SystemManager([])

    async def fake_systemctl(*args, timeout=20.0):
        return ""

    monkeypatch.setattr(manager, "_systemctl", fake_systemctl)

    with pytest.raises(ValidationError):
        await manager.reboot({"now": True})


@pytest.mark.asyncio
async def test_reboot_queues_systemctl_reboot(monkeypatch):
    manager = SystemManager([])
    calls = []

    async def fake_systemctl(*args, timeout=20.0):
        calls.append((args, timeout))
        return ""

    monkeypatch.setattr(manager, "_systemctl", fake_systemctl)

    result = await manager.reboot({})

    assert result == {"accepted": True}
    assert calls == [(("reboot",), 5.0)]
