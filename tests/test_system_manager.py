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
async def test_system_capabilities_advertise_transactional_live_mdns():
    manager = SystemManager([])

    assert await manager.capabilities({}) == {
        "hostname_live_mdns": True,
        "hostname_transactional": True,
        "recovery_local_hostname": "43a9-9ed7.local",
    }


@pytest.mark.asyncio
async def test_wait_for_avahi_hostname_reads_registered_fqdn(monkeypatch):
    manager = SystemManager([])

    async def fake_avahi_call(method):
        return "i 2" if method == "GetState" else 's "speed-timer.local"'

    monkeypatch.setattr(manager, "_avahi_call", fake_avahi_call)

    assert await manager._wait_for_avahi_hostname() == "speed-timer.local"


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
        if args[0] == "/usr/bin/hostname":
            return "old-timer\n"
        return ""

    monkeypatch.setattr(manager, "_run_command", fake_run_command)
    monkeypatch.setattr(
        manager,
        "_wait_for_avahi_hostname",
        lambda: _async_value("speed-timer.local"),
    )

    result = await manager.hostname_set({"hostname": "speed-timer"})

    assert result == {
        "hostname": "speed-timer",
        "local_hostname": "speed-timer.local",
        "mdns_updated": True,
        "recovery_local_hostname": "43a9-9ed7.local",
        "accepted": True,
    }
    assert calls == [
        (("/usr/bin/hostname",), 2.0),
        (("/usr/bin/hostnamectl", "set-hostname", "speed-timer"), 10.0),
        (("/usr/bin/avahi-set-host-name", "speed-timer"), 10.0),
    ]


@pytest.mark.asyncio
async def test_hostname_reports_avahi_failure_without_restarting_services(monkeypatch):
    manager = SystemManager([])
    calls = []

    async def fake_run_command(*args, timeout=20.0):
        calls.append(args)
        if args[0] == "/usr/bin/hostname":
            return "old-timer"
        if args == ("/usr/bin/avahi-set-host-name", "speed-timer"):
            raise NotFoundError("Avahi is unavailable")
        return ""

    monkeypatch.setattr(manager, "_run_command", fake_run_command)
    monkeypatch.setattr(
        manager,
        "_wait_for_avahi_hostname",
        lambda: _async_value("old-timer.local"),
    )

    with pytest.raises(ValidationError, match="original hostname was restored"):
        await manager.hostname_set({"hostname": "speed-timer"})

    assert calls == [
        ("/usr/bin/hostname",),
        ("/usr/bin/hostnamectl", "set-hostname", "speed-timer"),
        ("/usr/bin/avahi-set-host-name", "speed-timer"),
        ("/usr/bin/hostnamectl", "set-hostname", "old-timer"),
        ("/usr/bin/avahi-set-host-name", "old-timer"),
    ]


@pytest.mark.asyncio
async def test_hostname_collision_rolls_back_linux_and_avahi(monkeypatch):
    manager = SystemManager([])
    calls = []
    published_names = iter(["speed-timer-2.local", "old-timer.local"])

    async def fake_run_command(*args, timeout=20.0):
        calls.append(args)
        return "old-timer" if args[0] == "/usr/bin/hostname" else ""

    async def fake_wait_for_avahi_hostname():
        return next(published_names)

    monkeypatch.setattr(manager, "_run_command", fake_run_command)
    monkeypatch.setattr(manager, "_wait_for_avahi_hostname", fake_wait_for_avahi_hostname)

    with pytest.raises(ValidationError, match="already in use"):
        await manager.hostname_set({"hostname": "speed-timer"})

    assert calls[-2:] == [
        ("/usr/bin/hostnamectl", "set-hostname", "old-timer"),
        ("/usr/bin/avahi-set-host-name", "old-timer"),
    ]


@pytest.mark.asyncio
async def test_hostnamectl_timeout_attempts_full_rollback(monkeypatch):
    manager = SystemManager([])
    calls = []

    async def fake_run_command(*args, timeout=20.0):
        calls.append(args)
        if args[0] == "/usr/bin/hostname":
            return "old-timer"
        if args == ("/usr/bin/hostnamectl", "set-hostname", "speed-timer"):
            raise TimeoutError
        return ""

    monkeypatch.setattr(manager, "_run_command", fake_run_command)
    monkeypatch.setattr(
        manager,
        "_wait_for_avahi_hostname",
        lambda: _async_value("old-timer.local"),
    )

    with pytest.raises(ValidationError, match="original hostname was restored"):
        await manager.hostname_set({"hostname": "speed-timer"})

    assert calls[-2:] == [
        ("/usr/bin/hostnamectl", "set-hostname", "old-timer"),
        ("/usr/bin/avahi-set-host-name", "old-timer"),
    ]


async def _async_value(value):
    return value


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
