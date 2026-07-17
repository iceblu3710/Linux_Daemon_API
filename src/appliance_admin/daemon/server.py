from __future__ import annotations

import asyncio
import grp
import logging
import os
import pwd
import socket
import struct
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from appliance_admin.config import DaemonSettings
from appliance_admin.daemon.errors import (
    AdminError,
    AuthorizationError,
    ValidationError,
)
from appliance_admin.models import IPCError, IPCRequest, IPCResponse

Handler = Callable[[dict[str, Any]], Awaitable[Any]]
LOG = logging.getLogger(__name__)


class IPCServer:
    def __init__(self, settings: DaemonSettings, handlers: dict[str, Handler]):
        self.settings = settings
        self.handlers = handlers
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        path = self.settings.socket_path
        path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if path.exists() or path.is_socket():
            path.unlink()
        self.server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(path),
            limit=self.settings.max_request_bytes,
        )
        gid = grp.getgrnam(self.settings.socket_group).gr_gid
        os.chown(path, 0, gid)
        os.chmod(path, self.settings.socket_mode)
        LOG.info("Listening on %s", path)

    async def serve_forever(self) -> None:
        if self.server is None:
            await self.start()
        assert self.server is not None
        async with self.server:
            await self.server.serve_forever()

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        try:
            self.settings.socket_path.unlink(missing_ok=True)
        except OSError:
            LOG.exception("Could not remove socket")

    def _peer_credentials(self, writer: asyncio.StreamWriter) -> tuple[int, int, int]:
        sock = writer.get_extra_info("socket")
        if sock is None:
            raise AuthorizationError("Peer socket unavailable")
        raw = sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        pid, uid, gid = struct.unpack("3i", raw)
        return pid, uid, gid

    def _authorize(self, uid: int) -> None:
        if uid == 0 or uid in self.settings.allowed_uids:
            return
        try:
            user = pwd.getpwuid(uid)
            group = grp.getgrnam(self.settings.socket_group)
        except KeyError as exc:
            raise AuthorizationError("Unknown peer identity") from exc
        if user.pw_gid == group.gr_gid or user.pw_name in group.gr_mem:
            return
        raise AuthorizationError(f"UID {uid} is not allowed")

    async def _read_request_line(self, reader: asyncio.StreamReader) -> bytes:
        try:
            line = await asyncio.wait_for(
                reader.readline(), timeout=self.settings.request_timeout_seconds
            )
        except ValueError as exc:
            raise ValidationError("Malformed or oversized request") from exc
        if (
            not line
            or len(line) > self.settings.max_request_bytes
            or not line.endswith(b"\n")
        ):
            raise ValidationError("Malformed or oversized request")
        return line

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request_id = "unknown"
        pid = uid = gid = -1
        try:
            pid, uid, gid = self._peer_credentials(writer)
            self._authorize(uid)
            line = await self._read_request_line(reader)
            request = IPCRequest.model_validate_json(line)
            request_id = request.id
            handler = self.handlers.get(request.action)
            if handler is None:
                raise ValidationError("Unknown action")
            LOG.info(
                "action=%s peer_pid=%d peer_uid=%d audit_user=%r",
                request.action,
                pid,
                uid,
                request.audit_user,
            )
            result = await asyncio.wait_for(
                handler(request.params), timeout=self.settings.request_timeout_seconds
            )
            response = IPCResponse(id=request.id, ok=True, result=result)
        except PydanticValidationError as exc:
            response = IPCResponse(
                id=request_id,
                ok=False,
                error=IPCError(code="invalid_request", message=str(exc)),
            )
        except AdminError as exc:
            LOG.warning("Rejected request from pid=%d uid=%d: %s", pid, uid, exc)
            response = IPCResponse(
                id=request_id, ok=False, error=IPCError(code=exc.code, message=str(exc))
            )
        except TimeoutError:
            response = IPCResponse(
                id=request_id,
                ok=False,
                error=IPCError(code="timeout", message="Operation timed out"),
            )
        except Exception:
            LOG.exception("Unhandled daemon request error")
            response = IPCResponse(
                id=request_id,
                ok=False,
                error=IPCError(code="internal_error", message="Internal daemon error"),
            )
        try:
            encoded = response.model_dump_json().encode() + b"\n"
            writer.write(encoded)
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
