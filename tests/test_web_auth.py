import asyncio
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from appliance_admin.config import WebSettings
from appliance_admin.web.auth import admin_dependency

SECRET = "test-secret-that-is-at-least-32-characters"


def _settings() -> WebSettings:
    return WebSettings(auth_secret=SECRET, _env_file=None)


def _token(**overrides) -> str:
    claims = {
        "sub": "alice",
        "role": "admin",
        "iss": "appliance-admin",
        "aud": "appliance-admin-api",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _authenticate(token: str):
    dependency = admin_dependency(_settings())
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    return asyncio.run(dependency(credentials))


def test_accepts_valid_admin_jwt():
    user = _authenticate(_token())
    assert user.username == "alice"
    assert user.is_admin


def test_rejects_non_admin_role():
    with pytest.raises(HTTPException) as exc_info:
        _authenticate(_token(role="viewer"))
    assert exc_info.value.status_code == 403


def test_rejects_invalid_signature():
    token = jwt.encode(
        {
            "sub": "alice",
            "role": "admin",
            "iss": "appliance-admin",
            "aud": "appliance-admin-api",
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        "different-secret-that-is-also-long-enough",
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc_info:
        _authenticate(token)
    assert exc_info.value.status_code == 401
