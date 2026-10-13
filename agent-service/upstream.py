import json
from typing import Any

import httpx


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


async def request_json(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> tuple[int, Any]:
    """Bound response bytes, disable redirect credential forwarding, and mask upstream errors."""
    try:
        async with client.stream(method, url, follow_redirects=False, **kwargs) as response:
            if response.status_code >= 400:
                # Never read or return an upstream error body: it may contain secrets.
                return response.status_code, None
            if 300 <= response.status_code < 400:
                raise ServiceError(502, "upstream_redirect", "Upstream redirects are not allowed")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise ServiceError(502, "upstream_response_too_large", "Upstream response exceeded the allowed size")
            try:
                return response.status_code, json.loads(data)
            except (ValueError, UnicodeDecodeError):
                raise ServiceError(502, "invalid_upstream_response", "Upstream returned invalid JSON") from None
    except httpx.TimeoutException:
        raise ServiceError(504, "upstream_timeout", "A dependent service timed out") from None
    except httpx.RequestError:
        raise ServiceError(503, "upstream_unavailable", "A dependent service is unavailable") from None


def require_success(status: int, *, no_data: bool = False) -> None:
    if 200 <= status < 300:
        return
    if status == 404 and no_data:
        raise ServiceError(404, "usage_not_found", "No usage records exist for the requested month")
    if status == 429:
        raise ServiceError(503, "upstream_rate_limited", "A dependent service is temporarily rate limited")
    raise ServiceError(502, "upstream_error", "A dependent service could not complete the request")
