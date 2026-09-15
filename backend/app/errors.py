"""Error types that carry enough context to render a useful message in the UI."""

from __future__ import annotations

from typing import Any


class ProbeError(Exception):
    """Base for every failure the app raises deliberately."""

    status_code = 400
    code = "probe_error"

    def __init__(self, message: str, *, detail: Any = None, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            body["hint"] = self.hint
        if self.detail is not None:
            body["detail"] = self.detail
        return body


class ConfigurationError(ProbeError):
    status_code = 400
    code = "configuration_error"


class NotConfiguredError(ProbeError):
    status_code = 409
    code = "not_configured"


class UpstreamError(ProbeError):
    """A call to Jira, Elasticsearch or Kibana failed."""

    status_code = 502
    code = "upstream_error"

    def __init__(
        self,
        service: str,
        message: str,
        *,
        status: int | None = None,
        detail: Any = None,
        hint: str | None = None,
    ):
        super().__init__(message, detail=detail, hint=hint)
        self.service = service
        self.upstream_status = status

    def to_dict(self) -> dict[str, Any]:
        body = super().to_dict()
        body["service"] = self.service
        if self.upstream_status is not None:
            body["upstream_status"] = self.upstream_status
        return body


class LicenseError(ProbeError):
    """A feature exists but the Elastic licence does not permit it."""

    status_code = 402
    code = "license_required"

    def __init__(self, message: str, *, required: str, current: str | None = None):
        super().__init__(
            message,
            detail={"required_license": required, "current_license": current},
            hint="Ticket creation falls back to the direct Jira REST API.",
        )
        self.required = required
        self.current = current
