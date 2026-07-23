from __future__ import annotations

from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, status

from appliance_admin.config import WebSettings
from appliance_admin.ipc import DaemonClient, DaemonClientError
from appliance_admin.models import (
    NetworkStatus,
    NetworkProfile,
    ServiceActionRequest,
    ServiceStatus,
    WifiConnectRequest,
)
from appliance_admin.web.auth import WebUser, admin_dependency


def create_app(settings: WebSettings | None = None) -> FastAPI:
    settings = settings or WebSettings()
    client = DaemonClient(settings.daemon_socket, settings.daemon_timeout_seconds)
    app = FastAPI(title="Appliance Admin API", version="0.1.0")
    require_admin = admin_dependency(settings)

    async def invoke(action: str, params: dict, user: WebUser):
        try:
            return await client.call(action, params, audit_user=user.username)
        except DaemonClientError as exc:
            unavailable = exc.code in {
                "daemon_unavailable",
                "network_backend_unavailable",
                "timeout",
            }
            raise HTTPException(
                status_code=(
                    status.HTTP_503_SERVICE_UNAVAILABLE
                    if unavailable
                    else status.HTTP_400_BAD_REQUEST
                ),
                detail={"error": exc.code, "detail": str(exc)},
            ) from exc

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/admin/network/status", response_model=NetworkStatus)
    async def network_status(user: WebUser = Depends(require_admin)):
        return await invoke("network.status", {}, user)

    @app.get("/api/admin/wifi/scan")
    async def wifi_scan(
        force: bool = Query(default=False), user: WebUser = Depends(require_admin)
    ):
        return await invoke("wifi.scan", {"force": force}, user)

    @app.post("/api/admin/wifi/connect", status_code=status.HTTP_202_ACCEPTED)
    async def wifi_connect(
        body: WifiConnectRequest, user: WebUser = Depends(require_admin)
    ):
        return await invoke("wifi.connect", body.model_dump(), user)

    @app.post("/api/admin/wifi/disconnect", status_code=status.HTTP_202_ACCEPTED)
    async def wifi_disconnect(user: WebUser = Depends(require_admin)):
        return await invoke("wifi.disconnect", {}, user)

    @app.get("/api/admin/network/profiles", response_model=list[NetworkProfile])
    async def network_profiles(user: WebUser = Depends(require_admin)):
        result = await invoke("network.profiles", {}, user)
        return result["profiles"]

    @app.post(
        "/api/admin/network/profiles/{profile_uuid}/activate",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def activate_profile(
        profile_uuid: UUID, user: WebUser = Depends(require_admin)
    ):
        return await invoke(
            "network.profile.activate", {"uuid": str(profile_uuid)}, user
        )

    @app.delete("/api/admin/network/profiles/{profile_uuid}")
    async def delete_profile(
        profile_uuid: UUID, user: WebUser = Depends(require_admin)
    ):
        return await invoke(
            "network.profile.delete", {"uuid": str(profile_uuid)}, user
        )

    @app.get("/api/admin/services/{service:path}", response_model=ServiceStatus)
    async def service_status(service: str, user: WebUser = Depends(require_admin)):
        body = ServiceActionRequest(service=service)
        return await invoke("service.status", body.model_dump(), user)

    @app.post(
        "/api/admin/services/{service:path}/{action}",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def service_action(
        service: str, action: str, user: WebUser = Depends(require_admin)
    ):
        if action not in {"start", "stop", "restart"}:
            raise HTTPException(status_code=404, detail="Unknown service action")
        body = ServiceActionRequest(service=service)
        return await invoke(f"service.{action}", body.model_dump(), user)

    @app.post("/api/admin/system/reboot", status_code=status.HTTP_202_ACCEPTED)
    async def reboot(user: WebUser = Depends(require_admin)):
        return await invoke("system.reboot", {}, user)

    @app.post("/api/admin/system/poweroff", status_code=status.HTTP_202_ACCEPTED)
    async def poweroff(user: WebUser = Depends(require_admin)):
        return await invoke("system.poweroff", {}, user)

    return app


app = create_app()
