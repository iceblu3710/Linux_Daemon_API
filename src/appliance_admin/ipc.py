from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from appliance_admin.models import IPCRequest, IPCResponse


class DaemonClientError(RuntimeError):
    pass


class DaemonClient:
    def __init__(self, socket_path: Path, timeout: float = 35.0, max_bytes: int = 65536):
        self.socket_path = socket_path
        self.timeout = timeout
        self.max_bytes = max_bytes

    async def call(
        self, action: str, params: dict[str, Any] | None = None, audit_user: str | None = None
    ) -> Any:
        request = IPCRequest(action=action, params=params or {}, audit_user=audit_user)
        payload = request.model_dump_json().encode() + b"\n"
        if len(payload) > self.max_bytes:
            raise DaemonClientError("Request is too large")

        async def exchange() -> IPCResponse:
            reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
            try:
                writer.write(payload)
                await writer.drain()
                line = await reader.readline()
                if not line or len(line) > self.max_bytes:
                    raise DaemonClientError("Invalid response from daemon")
                return IPCResponse.model_validate_json(line)
            finally:
                writer.close()
                await writer.wait_closed()

        try:
            response = await asyncio.wait_for(exchange(), timeout=self.timeout)
        except (TimeoutError, OSError, ValueError) as exc:
            raise DaemonClientError(f"Daemon communication failed: {exc}") from exc
        if not response.ok:
            message = response.error.message if response.error else "Unknown daemon error"
            raise DaemonClientError(message)
        return response.result
