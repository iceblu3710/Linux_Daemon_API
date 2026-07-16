from __future__ import annotations

import asyncio
import re

from appliance_admin.daemon.errors import NotFoundError, ValidationError
from appliance_admin.models import HostnameSetRequest, ServiceActionRequest, ServiceStatus

_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")


class SystemManager:
    def __init__(self, allowed_services: list[str]):
        self.allowed_services = frozenset(allowed_services)

    def _validate_service(self, raw: str) -> str:
        request = ServiceActionRequest(service=raw)
        service = request.service
        if not _SERVICE_RE.fullmatch(service):
            raise ValidationError("Invalid service unit name")
        if service not in self.allowed_services:
            raise ValidationError("Service is not in the daemon allowlist")
        return service

    async def _systemctl(self, *args: str, timeout: float = 20.0) -> str:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/systemctl",
            "--no-pager",
            "--no-ask-password",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            message = stderr.decode(errors="replace").strip() or "systemctl failed"
            raise NotFoundError(message[:500])
        return stdout.decode(errors="replace").strip()

    async def service_restart(self, params: dict) -> dict:
        service = self._validate_service(str(params.get("service", "")))
        await self._systemctl("restart", service)
        return {"service": service, "accepted": True}

    async def service_start(self, params: dict) -> dict:
        service = self._validate_service(str(params.get("service", "")))
        await self._systemctl("start", service)
        return {"service": service, "accepted": True}

    async def service_stop(self, params: dict) -> dict:
        service = self._validate_service(str(params.get("service", "")))
        await self._systemctl("stop", service)
        return {"service": service, "accepted": True}

    async def service_status(self, params: dict) -> dict:
        service = self._validate_service(str(params.get("service", "")))
        output = await self._systemctl(
            "show",
            service,
            "--property=ActiveState,SubState,UnitFileState",
        )
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        return ServiceStatus(
            service=service,
            active_state=values.get("ActiveState", "unknown"),
            sub_state=values.get("SubState", "unknown"),
            unit_file_state=values.get("UnitFileState") or None,
        ).model_dump()

    async def reboot(self, params: dict) -> dict:
        if params:
            raise ValidationError("Reboot does not accept parameters")
        # Return is normally lost as systemd begins shutdown. The API treats acceptance as success.
        await self._systemctl("reboot", timeout=5.0)
        return {"accepted": True}

    async def hostname_set(self, params: dict) -> dict:
        request = HostnameSetRequest.model_validate(params)
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/hostnamectl",
            "set-hostname",
            request.hostname,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            message = stderr.decode(errors="replace").strip() or "hostnamectl failed"
            raise ValidationError(message[:500])
        return {
            "hostname": request.hostname,
            "accepted": True,
            "output": stdout.decode(errors="replace").strip(),
        }
