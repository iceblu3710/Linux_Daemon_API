from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class IPCRequest(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    action: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    audit_user: str | None = Field(default=None, max_length=128)


class IPCError(BaseModel):
    code: str
    message: str


class IPCResponse(BaseModel):
    id: str
    ok: bool
    result: Any | None = None
    error: IPCError | None = None


class WifiAccessPoint(BaseModel):
    ssid: str
    bssid: str
    strength: int = Field(ge=0, le=100)
    frequency_mhz: int
    secured: bool
    active: bool = False


class InterfaceAddress(BaseModel):
    address: str
    prefix: int


class NetworkInterface(BaseModel):
    name: str
    kind: Literal["wifi", "ethernet", "other"]
    state: str
    carrier: bool | None = None
    active_connection: str | None = None
    ipv4: list[InterfaceAddress] = Field(default_factory=list)
    gateway4: str | None = None
    dns4: list[str] = Field(default_factory=list)


class NetworkStatus(BaseModel):
    connectivity: str
    primary_connection: str | None = None
    wifi_ssid: str | None = None
    interfaces: list[NetworkInterface] = Field(default_factory=list)


class WifiConnectRequest(BaseModel):
    ssid: str = Field(min_length=1, max_length=32)
    password: str | None = Field(default=None, min_length=8, max_length=63)
    hidden: bool = False


class ServiceActionRequest(BaseModel):
    service: str = Field(min_length=1, max_length=128)


class ServiceStatus(BaseModel):
    service: str
    active_state: str
    sub_state: str
    unit_file_state: str | None = None
