from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from appliance_admin.config import WebSettings


@dataclass(frozen=True)
class WebUser:
    username: str
    is_admin: bool


_bearer = HTTPBearer(auto_error=False)


def admin_dependency(settings: WebSettings):
    async def require_admin(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(_bearer)
        ] = None,
    ) -> WebUser:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Admin login required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            claims = jwt.decode(
                credentials.credentials,
                settings.auth_secret.get_secret_value(),
                algorithms=["HS256"],
                issuer=settings.auth_issuer,
                audience=settings.auth_audience,
                options={"require": ["exp", "sub", "role"]},
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        username = claims.get("sub")
        if not isinstance(username, str) or not username or claims.get("role") != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
        return WebUser(username=username[:128], is_admin=True)

    return require_admin
