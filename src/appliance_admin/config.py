from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DaemonSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APPLIANCE_ADMIN_", env_file="/etc/appliance-admin/daemon.env", extra="ignore"
    )

    socket_path: Path = Path("/run/appliance-admin/admin.sock")
    socket_mode: int = 0o660
    socket_group: str = "appliance-web"
    allowed_uids: list[int] = Field(default_factory=list)
    allowed_services: list[str] = Field(default_factory=lambda: ["ninja-timer.service"])
    scan_min_interval_seconds: float = 12.0
    scan_cache_seconds: float = 8.0
    request_timeout_seconds: float = 30.0
    max_request_bytes: int = 65536
    log_level: str = "INFO"


class WebSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APPLIANCE_WEB_", env_file="/etc/appliance-admin/web.env", extra="ignore"
    )

    daemon_socket: Path = Path("/run/appliance-admin/admin.sock")
    daemon_timeout_seconds: float = 35.0
    bind_host: str = "127.0.0.1"
    bind_port: int = 8088
    trusted_proxy_headers: bool = False
    auth_secret: SecretStr = Field(min_length=32)
    auth_issuer: str = "appliance-admin"
    auth_audience: str = "appliance-admin-api"
    log_level: str = "INFO"
