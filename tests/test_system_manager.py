import pytest

from appliance_admin.daemon.errors import ValidationError
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
