from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, Request, status


@dataclass(frozen=True)
class WebUser:
    username: str
    is_admin: bool


async def require_admin(
    request: Request,
    x_authenticated_user: str | None = Header(default=None),
    x_authenticated_role: str | None = Header(default=None),
) -> WebUser:
    """Integration seam for your real session/auth middleware.

    The example accepts identity headers only from loopback, making it suitable behind a
    same-host reverse proxy that strips and sets these headers. Replace this dependency with
    your existing login/session check before exposing the API beyond localhost.
    """
    client = request.client.host if request.client else ""
    if client not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Untrusted proxy")
    if not x_authenticated_user or x_authenticated_role != "admin":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin login required")
    return WebUser(username=x_authenticated_user[:128], is_admin=True)
