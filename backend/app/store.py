"""Encrypted on-disk store for connection settings.

Deliberately a single JSON file rather than a database: the app stores one
connection profile, and a file keeps the deployment story to "run the process".
Secret fields are encrypted individually so the file can be inspected safely.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from .crypto import SecretBox, mask
from .models import (
    ConnectionState,
    ConnectorSettings,
    ElasticCredentials,
    JiraCredentials,
    MaskedElastic,
    MaskedJira,
    SetupRequest,
)

SECRET_FIELDS = {
    "jira": {"api_token"},
    "elastic": {"api_key", "password"},
}


class ConnectionStore:
    def __init__(self, path: str, box: SecretBox):
        self._path = Path(path)
        self._box = box
        self._lock = Lock()

    # -- persistence -------------------------------------------------------
    def _read_raw(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_raw(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write via a temp file in the same directory so a crash mid-write
        # cannot leave a half-written credentials file behind.
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self._path)
            os.chmod(self._path, 0o600)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- api ---------------------------------------------------------------
    def is_configured(self) -> bool:
        return bool(self._read_raw().get("jira"))

    def save(self, request: SetupRequest) -> None:
        with self._lock:
            payload = {
                "elastic": self._encrypt_section(
                    request.elastic.model_dump(), SECRET_FIELDS["elastic"]
                ),
                "jira": self._encrypt_section(
                    request.jira.model_dump(), SECRET_FIELDS["jira"]
                ),
                "connector": request.connector.model_dump(),
                "ticket_route": request.ticket_route,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_raw(payload)

    def clear(self) -> None:
        with self._lock:
            self._path.unlink(missing_ok=True)

    def _encrypt_section(
        self, section: dict[str, Any], secret_keys: set[str]
    ) -> dict[str, Any]:
        return {
            k: (self._box.encrypt(v) if k in secret_keys and isinstance(v, str) else v)
            for k, v in section.items()
        }

    def _decrypt_section(
        self, section: dict[str, Any], secret_keys: set[str]
    ) -> dict[str, Any]:
        return {
            k: (self._box.decrypt(v) if k in secret_keys and isinstance(v, str) else v)
            for k, v in section.items()
        }

    def jira(self) -> JiraCredentials | None:
        raw = self._read_raw().get("jira")
        if not raw:
            return None
        return JiraCredentials(**self._decrypt_section(raw, SECRET_FIELDS["jira"]))

    def elastic(self) -> ElasticCredentials | None:
        raw = self._read_raw().get("elastic")
        if not raw:
            return None
        return ElasticCredentials(
            **self._decrypt_section(raw, SECRET_FIELDS["elastic"])
        )

    def connector(self) -> ConnectorSettings | None:
        raw = self._read_raw().get("connector")
        return ConnectorSettings(**raw) if raw else None

    def ticket_route(self) -> str:
        return self._read_raw().get("ticket_route", "auto")

    def state(self) -> ConnectionState:
        """The safe, maskable view handed back to the browser."""
        raw = self._read_raw()
        if not raw.get("jira"):
            return ConnectionState(configured=False)

        jira = self.jira()
        elastic = self.elastic()
        assert jira is not None and elastic is not None
        return ConnectionState(
            configured=True,
            jira=MaskedJira(
                base_url=jira.base_url,
                account_email=jira.account_email,
                api_token=mask(jira.api_token),
                project_key=jira.project_key,
                default_issue_type=jira.default_issue_type,
            ),
            elastic=MaskedElastic(
                es_url=elastic.es_url,
                kibana_url=elastic.kibana_url,
                api_key=mask(elastic.api_key),
                username=elastic.username,
                password=mask(elastic.password) if elastic.password else "",
                verify_tls=elastic.verify_tls,
            ),
            connector=self.connector(),
            ticket_route=raw.get("ticket_route", "auto"),
            updated_at=raw.get("updated_at"),
        )
