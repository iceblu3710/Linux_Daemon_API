from __future__ import annotations

import asyncio
import logging
import signal

from appliance_admin.config import DaemonSettings
from appliance_admin.daemon.errors import NetworkBackendUnavailableError
from appliance_admin.daemon.managers.network import NetworkManager
from appliance_admin.daemon.managers.system import SystemManager
from appliance_admin.daemon.server import IPCServer


async def run() -> None:
    settings = DaemonSettings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    network = NetworkManager(
        settings.scan_min_interval_seconds,
        settings.scan_cache_seconds,
        wifi_interface=settings.wifi_interface,
        checkpoint_timeout=settings.network_checkpoint_timeout_seconds,
        activation_timeout=settings.network_activation_timeout_seconds,
    )
    system = SystemManager(settings.allowed_services)
    try:
        ready = await network.readiness()
        logging.getLogger(__name__).info(
            "Network backend ready: interface=%s state=%s",
            ready["interface"],
            ready["state"],
        )
    except NetworkBackendUnavailableError as exc:
        # Keep the socket available so callers receive the typed backend error;
        # NetworkManager/device state may recover without restarting this daemon.
        logging.getLogger(__name__).error("Network backend is not ready: %s", exc)
    handlers = {
        "network.status": network.status,
        "wifi.scan": network.scan,
        "wifi.connect": network.connect,
        "wifi.disconnect": network.disconnect,
        "network.profiles": network.profiles,
        "network.profile.activate": network.activate_profile,
        "network.profile.delete": network.delete_profile,
        "system.capabilities": system.capabilities,
        "system.reboot": system.reboot,
        "hostname.set": system.hostname_set,
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
