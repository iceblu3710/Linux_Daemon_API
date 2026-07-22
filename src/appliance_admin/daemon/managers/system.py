from __future__ import annotations

import asyncio
import re

from appliance_admin.daemon.errors import NotFoundError, ValidationError
from appliance_admin.models import (
    HostnameSetRequest,
    ServiceActionRequest,
    ServiceStatus,
)

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
        return await self._run_command(
            "/usr/bin/systemctl",
            "--no-pager",
            "--no-ask-password",
            *args,
            timeout=timeout,
        )

    async def _run_command(self, *args: str, timeout: float = 20.0) -> str:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            command = args[0].rsplit("/", 1)[-1] if args else "command"
            message = stderr.decode(errors="replace").strip() or f"{command} failed"
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
        try:
            await self._run_command(
                "/usr/bin/hostnamectl",
                "set-hostname",
                request.hostname,
                timeout=10.0,
            )
        except (NotFoundError, TimeoutError) as exc:
            raise ValidationError(f"Failed to set Linux hostname: {exc}") from exc

        # hostnamectl updates both the live kernel hostname and /etc/hostname,
        # but a running Avahi daemon keeps its own mDNS server name. Update it
        # in place so the new .local name is announced without interrupting the
        # independently published immutable recovery hostname.
        try:
            await self._run_command(
                "/usr/bin/avahi-set-host-name",
                request.hostname,
                timeout=10.0,
            )
        except (NotFoundError, TimeoutError) as exc:
            raise ValidationError(
                "Linux hostname changed, but the mDNS hostname could not be updated: "
                f"{exc}"
            ) from exc

        return {
            "hostname": request.hostname,
            "local_hostname": f"{request.hostname}.local",
            "mdns_updated": True,
            "accepted": True,
        }
