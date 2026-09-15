"""Application settings, loaded from the environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # This application
    probe_secret_key: str = ""
    probe_api_key: str = ""
    probe_state_path: str = "./probe-state.json"
    probe_cors_origins: str = "http://localhost:5173"
    probe_ticket_route: str = "auto"

    # Elasticsearch
    elastic_es_url: str = "http://localhost:9200"
    elastic_api_key: str = ""
    elastic_username: str = ""
    elastic_password: str = ""
    elastic_verify_tls: bool = True
    elastic_ca_cert_path: str = ""

    # Kibana
    kibana_url: str = "http://localhost:5601"
    kibana_api_key: str = ""

    # Content connector
    es_connector_id: str = "probe-jira-connector"
    es_connector_index: str = "search-jira-probe"
    es_connector_name: str = "Probe Jira content connector"
    es_connector_service_type: str = "jira"
    es_connector_sync_interval: str = "0 0 */3 * * ?"

    # Jira form defaults
    jira_base_url: str = ""
    jira_account_email: str = ""
    jira_api_token: str = ""
    jira_project_key: str = ""
    jira_default_issue_type: str = "Task"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.probe_cors_origins.split(",") if o.strip()]

    @property
    def tls_verify(self) -> bool | str:
        """What to hand httpx as ``verify``: a CA path, or a bool."""
        if self.elastic_ca_cert_path:
            return self.elastic_ca_cert_path
        return self.elastic_verify_tls


@lru_cache
def get_settings() -> Settings:
    return Settings()
