"""Shared HTTP plumbing for the Jira, Elasticsearch and Kibana clients."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..errors import UpstreamError

log = logging.getLogger("probe.http")

RETRY_STATUSES = {429, 502, 503, 504}
MAX_ATTEMPTS = 3


class BaseClient:
    """Thin wrapper adding retries, timeouts and uniform error translation.

    Every upstream failure becomes an :class:`UpstreamError` carrying the
    service name and the upstream status, so routers never have to know which
    HTTP library raised what.
    """

    service = "upstream"

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        verify: bool | str = True,
        timeout: float = 30.0,
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers or {},
            auth=auth,
            verify=verify,
            timeout=timeout,
            follow_redirects=True,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        expected: tuple[int, ...] | None = None,
        allow_statuses: tuple[int, ...] = (),
    ) -> httpx.Response:
        last_exc: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.request(
                    method, path, json=json_body, params=params
                )
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt == MAX_ATTEMPTS:
                    raise UpstreamError(
                        self.service,
                        f"{self.service} timed out after {MAX_ATTEMPTS} attempts.",
                        hint="Check the URL and that the service is reachable.",
                    ) from exc
                await asyncio.sleep(2**attempt * 0.5)
                continue
            except httpx.HTTPError as exc:
                raise UpstreamError(
                    self.service,
                    f"Could not reach {self.service}: {exc}",
                    hint="Check the URL, network access and TLS settings.",
                ) from exc

            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                delay = _retry_after(response, attempt)
                log.warning(
                    "%s returned %s, retrying in %.1fs (attempt %s/%s)",
                    self.service,
                    response.status_code,
                    delay,
                    attempt,
                    MAX_ATTEMPTS,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code in allow_statuses:
                return response
            if expected and response.status_code not in expected:
                raise self._error_for(response)
            if not expected and response.status_code >= 400:
                raise self._error_for(response)
            return response

        raise UpstreamError(
            self.service, f"{self.service} request failed."
        ) from last_exc

    def _error_for(self, response: httpx.Response) -> UpstreamError:
        return UpstreamError(
            self.service,
            summarise_error(response, self.service),
            status=response.status_code,
            detail=_safe_body(response),
        )

    # -- convenience -------------------------------------------------------
    async def get_json(self, path: str, **kw) -> Any:
        return (await self.request("GET", path, **kw)).json()

    async def post_json(self, path: str, body: Any = None, **kw) -> Any:
        response = await self.request("POST", path, json_body=body, **kw)
        return _json_or_none(response)

    async def put_json(self, path: str, body: Any = None, **kw) -> Any:
        response = await self.request("PUT", path, json_body=body, **kw)
        return _json_or_none(response)


def _json_or_none(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:2000]}


def _retry_after(response: httpx.Response, attempt: int) -> float:
    """Honour Retry-After when the service sends it, else exponential backoff."""
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), 30.0)
        except ValueError:
            pass
    return 2**attempt * 0.5


def _safe_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:1000]}


def summarise_error(response: httpx.Response, service: str) -> str:
    """Pull the human-readable part out of a Jira / Elastic error body.

    The three services shape errors differently, and the useful sentence is
    buried in a different key in each.
    """
    body = _safe_body(response)
    status = response.status_code

    if isinstance(body, dict):
        # Jira: {"errorMessages": [...], "errors": {"field": "msg"}}
        messages = body.get("errorMessages") or []
        field_errors = body.get("errors") or {}
        if isinstance(field_errors, dict) and field_errors:
            messages = list(messages) + [
                f"{k}: {v}" for k, v in field_errors.items()
            ]
        if messages:
            return f"{service} returned {status}: " + "; ".join(str(m) for m in messages)

        # Elasticsearch: {"error": {"reason": "...", "type": "..."}}
        error = body.get("error")
        if isinstance(error, dict) and error.get("reason"):
            return f"{service} returned {status}: {error['reason']}"
        if isinstance(error, str):
            return f"{service} returned {status}: {error}"

        # Kibana: {"message": "...", "error": "...", "statusCode": n}
        if body.get("message"):
            return f"{service} returned {status}: {body['message']}"

    return f"{service} returned HTTP {status}."
