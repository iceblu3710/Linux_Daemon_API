import asyncio

import pytest

from appliance_admin.config import DaemonSettings
from appliance_admin.daemon.errors import ValidationError
from appliance_admin.daemon.server import IPCServer


def test_oversized_line_uses_validation_error_path():
    async def run_test():
        settings = DaemonSettings(max_request_bytes=8)
        server = IPCServer(settings, {})
        reader = asyncio.StreamReader(limit=settings.max_request_bytes)
        reader.feed_data(b"123456789\n")
        reader.feed_eof()

        with pytest.raises(ValidationError, match="oversized"):
            await server._read_request_line(reader)

    asyncio.run(run_test())


def test_request_at_limit_is_accepted():
    async def run_test():
        settings = DaemonSettings(max_request_bytes=8)
        server = IPCServer(settings, {})
        reader = asyncio.StreamReader(limit=settings.max_request_bytes)
        reader.feed_data(b"1234567\n")
        reader.feed_eof()

        assert await server._read_request_line(reader) == b"1234567\n"

    asyncio.run(run_test())
