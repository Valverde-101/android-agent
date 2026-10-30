"""FastAPI route wrapper for AndroidBuild's shared device lease.

Phone dashboard requests and MCP calls use the same on-disk Core lock.  The
wrapper only leases requests that identify one device, leaving inventory and
configuration endpoints untouched.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute

from gitd.integrations.androidbuild_device_lease import androidbuild_device_lease

_DEVICE_BODY_ROUTES = frozenset(
    {
        "/api/phone/input",
        "/api/phone/tap",
        "/api/phone/type",
        "/api/phone/clipboard",
        "/api/phone/paste-text",
        "/api/phone/back",
        "/api/phone/key",
        "/api/phone/launch",
        "/api/phone/browser/open-url",
        "/api/phone/browser/search",
        "/api/phone/browser/back",
        "/api/phone/browser/wait-for-text",
        "/api/phone/browser/read-news",
        "/api/phone/force-stop",
        "/api/phone/app-state",
        "/api/phone/swipe",
        "/api/phone/notifications/open",
        "/api/phone/notifications/clear",
        "/api/phone/recording/start",
        "/api/phone/recording/stop",
        "/api/phone/wireless/enable",
        "/api/phone/wireless/disconnect",
    }
)


def _device_from_request(request: Request, body: bytes) -> str:
    """Get a device serial from a path parameter or a JSON request body."""
    serial = request.path_params.get("device")
    if serial:
        return str(serial).strip()

    if not body:
        return ""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" not in content_type:
        return ""
    try:
        payload: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("device")
    return str(value).strip() if value is not None else ""


class AndroidBuildLeaseRoute(APIRoute):
    """Acquire Core's exclusive lease around a finite device-scoped HTTP call."""

    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def leased_handler(request: Request):
            # Request.body() is cached by Starlette, so FastAPI can still parse
            # the same JSON payload in the original route handler.
            body = await request.body()
            serial = _device_from_request(request, body)
            if not serial:
                if self.path in _DEVICE_BODY_ROUTES:
                    raise HTTPException(status_code=400, detail="device serial required")
                return await original_handler(request)

            try:
                with androidbuild_device_lease(
                    serial,
                    repository="Valverde-101/android-agent",
                    run_id=f"dashboard-rest-{request.method.lower()}",
                ):
                    return await original_handler(request)
            except RuntimeError as error:
                detail = str(error)
                if detail.startswith("DEVICE_BUSY"):
                    raise HTTPException(status_code=409, detail=detail) from error
                if detail.startswith("DEVICE_LEASE=BLOCKED"):
                    raise HTTPException(status_code=501, detail=detail) from error
                raise

        return leased_handler
