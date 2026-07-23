from __future__ import annotations

import asyncio
import re
import shlex

from appliance_admin.daemon.errors import NotFoundError, ValidationError
from appliance_admin.models import (
    HostnameSetRequest,
    ServiceActionRequest,
    ServiceStatus,
)

_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")
_RECOVERY_HOSTNAME = "43a9-9ed7"
_RECOVERY_LOCAL_HOSTNAME = f"{_RECOVERY_HOSTNAME}.local"
_AVAHI_DESTINATION = "org.freedesktop.Avahi"
_AVAHI_PATH = "/"
_AVAHI_INTERFACE = "org.freedesktop.Avahi.Server"
_AVAHI_RUNNING = 2


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
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
            )
        except FileNotFoundError as exc:
            command = args[0] if args else "command"
            raise NotFoundError(f"Required executable not found: {command}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            command = args[0].rsplit("/", 1)[-1] if args else "command"
            message = stderr.decode(errors="replace").strip() or f"{command} failed"
            raise NotFoundError(message[:500])
        return stdout.decode(errors="replace").strip()

    async def _avahi_call(self, method: str) -> str:
        return await self._run_command(
            "/usr/bin/busctl",
            "--system",
            "call",
            _AVAHI_DESTINATION,
            _AVAHI_PATH,
            _AVAHI_INTERFACE,
            method,
            timeout=2.0,
        )

    async def _wait_for_avahi_hostname(self, timeout: float = 4.0) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        last_error: Exception | None = None
        while True:
            try:
                state_parts = shlex.split(await self._avahi_call("GetState"))
                state = int(state_parts[-1]) if state_parts else -1
                if state == _AVAHI_RUNNING:
                    hostname_parts = shlex.split(await self._avahi_call("GetHostNameFqdn"))
                    if len(hostname_parts) == 2 and hostname_parts[0] == "s":
                        return hostname_parts[1].rstrip(".").lower()
            except (NotFoundError, TimeoutError, ValueError) as exc:
                last_error = exc

            if asyncio.get_running_loop().time() >= deadline:
                detail = f": {last_error}" if last_error else ""
                raise ValidationError(f"Avahi did not finish registering its hostname{detail}")
            await asyncio.sleep(0.1)

    async def _restore_hostname(self, hostname: str) -> list[str]:
        rollback_errors: list[str] = []
        try:
            await self._run_command(
                "/usr/bin/hostnamectl",
                "set-hostname",
                hostname,
                timeout=10.0,
            )
        except (NotFoundError, TimeoutError) as exc:
            rollback_errors.append(f"Linux rollback failed: {exc}")
        try:
            await self._run_command(
                "/usr/bin/avahi-set-host-name",
                hostname,
                timeout=10.0,
            )
            restored_hostname = await self._wait_for_avahi_hostname()
            if restored_hostname != f"{hostname}.local":
                raise ValidationError(f"Avahi restored the unexpected hostname {restored_hostname}")
        except (NotFoundError, TimeoutError, ValidationError) as exc:
            rollback_errors.append(f"mDNS rollback failed: {exc}")
        return rollback_errors

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

    async def poweroff(self, params: dict) -> dict:
        if params:
            raise ValidationError("Poweroff does not accept parameters")
        # systemd performs an orderly service stop and filesystem unmount before poweroff.
        await self._systemctl("poweroff", timeout=5.0)
        return {"accepted": True}

    async def capabilities(self, params: dict) -> dict:
        if params:
            raise ValidationError("System capabilities do not accept parameters")
        return {
            "hostname_live_mdns": True,
            "hostname_transactional": True,
            "recovery_local_hostname": _RECOVERY_LOCAL_HOSTNAME,
        }

    async def hostname_set(self, params: dict) -> dict:
        request = HostnameSetRequest.model_validate(params)
        requested_hostname = request.hostname.lower()
        try:
            original_hostname = (
                (await self._run_command("/usr/bin/hostname", timeout=2.0)).strip().lower()
            )
        except (NotFoundError, TimeoutError) as exc:
            raise ValidationError(f"Failed to read the current Linux hostname: {exc}") from exc

        try:
            await self._run_command(
                "/usr/bin/hostnamectl",
                "set-hostname",
                requested_hostname,
                timeout=10.0,
            )
        except (NotFoundError, TimeoutError) as exc:
            rollback_errors = await self._restore_hostname(original_hostname)
            rollback_status = (
                "; ".join(rollback_errors)
                if rollback_errors
                else "the original hostname was restored"
            )
            raise ValidationError(
                f"Failed to set Linux hostname: {exc}; {rollback_status}"
            ) from exc

        # hostnamectl updates both the live kernel hostname and /etc/hostname,
        # but a running Avahi daemon keeps its own mDNS server name. Update it
        # in place so the new .local name is announced without interrupting the
        # independently published immutable recovery hostname.
        try:
            await self._run_command(
                "/usr/bin/avahi-set-host-name",
                requested_hostname,
                timeout=10.0,
            )
            published_hostname = await self._wait_for_avahi_hostname()
            expected_local_hostname = f"{requested_hostname}.local"
            if published_hostname != expected_local_hostname:
                raise ValidationError(
                    "The requested mDNS hostname is already in use; Avahi selected "
                    f"{published_hostname} instead"
                )
        except (NotFoundError, TimeoutError, ValidationError) as exc:
            rollback_errors = await self._restore_hostname(original_hostname)
            rollback_status = (
                "; ".join(rollback_errors)
                if rollback_errors
                else "the original hostname was restored"
            )
            raise ValidationError(
                f"Failed to publish the requested hostname: {exc}; {rollback_status}"
            ) from exc

        return {
            "hostname": requested_hostname,
            "local_hostname": f"{requested_hostname}.local",
            "mdns_updated": True,
            "recovery_local_hostname": _RECOVERY_LOCAL_HOSTNAME,
            "accepted": True,
        }
