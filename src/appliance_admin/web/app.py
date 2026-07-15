from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Query, status

from appliance_admin.config import WebSettings
from appliance_admin.ipc import DaemonClient, DaemonClientError
from appliance_admin.models import NetworkStatus, ServiceActionRequest, ServiceStatus, WifiConnectRequest
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
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
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
    async def wifi_connect(body: WifiConnectRequest, user: WebUser = Depends(require_admin)):
        return await invoke("wifi.connect", body.model_dump(), user)

    @app.get("/api/admin/services/{service:path}", response_model=ServiceStatus)
    async def service_status(service: str, user: WebUser = Depends(require_admin)):
        body = ServiceActionRequest(service=service)
        return await invoke("service.status", body.model_dump(), user)

    @app.post("/api/admin/services/{service:path}/{action}", status_code=status.HTTP_202_ACCEPTED)
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

    return app


app = create_app()
