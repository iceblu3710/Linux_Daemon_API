from __future__ import annotations

import asyncio
import logging
import signal

from appliance_admin.config import DaemonSettings
from appliance_admin.daemon.managers.network import NetworkManager
from appliance_admin.daemon.managers.system import SystemManager
from appliance_admin.daemon.server import IPCServer


async def run() -> None:
    settings = DaemonSettings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    network = NetworkManager(settings.scan_min_interval_seconds, settings.scan_cache_seconds)
    system = SystemManager(settings.allowed_services)
    handlers = {
        "network.status": network.status,
        "wifi.scan": network.scan,
        "wifi.connect": network.connect,
        "system.reboot": system.reboot,
        "service.status": system.service_status,
        "service.start": system.service_start,
        "service.stop": system.service_stop,
        "service.restart": system.service_restart,
    }
    server = IPCServer(settings, handlers)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await server.start()
    serve_task = asyncio.create_task(server.serve_forever())
    await stop.wait()
    serve_task.cancel()
    await server.close()
    await network.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
