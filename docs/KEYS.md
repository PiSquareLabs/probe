# Keys, credentials and settings

Every secret and setting the app reads, what it is for, **whether it is obtainable on
a free tier**, and an **assumed placeholder value** to develop against.

> All placeholder values below are fabricated for local development. They are
> syntactically shaped like the real thing so that format validation can be exercised,
> but none of them authenticate against anything. Replace them before any real use.
> `backend/.env.example` is the machine-readable copy of this table.

---

## 1. Jira Cloud

| Key | Purpose | Free tier? | Assumed value |
|---|---|---|---|
| `JIRA_BASE_URL` | Jira Cloud site root. Used for both the REST API and the connector's `jira_url`. | ✅ Free plan (10 users) | `https://probe-demo.atlassian.net` |
| `JIRA_ACCOUNT_EMAIL` | Atlassian account email; the username half of Basic auth. | ✅ | `probe-bot@example.com` |
| `JIRA_API_TOKEN` | API token from <https://id.atlassian.com/manage-profile/security/api-tokens>. Password half of Basic auth. | ✅ Free plan can mint tokens | `ATATT3xFfGF0T_pR0b3_dEv_pLaCeHoLdEr_t0k3n_9c1f4e2a` |
| `JIRA_PROJECT_KEY` | Default project new tickets land in. | ✅ | `PROBE` |
| `JIRA_DEFAULT_ISSUE_TYPE` | Issue type used when the caller does not name one. | ✅ | `Task` |

**How the token is presented:** `Authorization: Basic base64(JIRA_ACCOUNT_EMAIL:JIRA_API_TOKEN)`.

**Permissions the account must hold in `JIRA_PROJECT_KEY`:** `BROWSE_PROJECTS` and
`CREATE_ISSUES`. The app pre-flights these at
`GET /rest/api/3/mypermissions` and refuses to finish setup without them.

**Scopes:** none to configure. API-token Basic auth ignores OAuth scopes entirely —
the token inherits the account's permissions. Scopes (`write:jira-work`,
`read:jira-work`, or granular `write:issue:jira` / `read:issue:jira` /
`read:project:jira`) would only apply if this were reworked onto OAuth 2.0 (3LO).

**Not required:** no Marketplace app, no paid add-on, no Jira Service Management
licence, no Forge/Connect app registration.

## 2. Elasticsearch

| Key | Purpose | Free tier? | Assumed value |
|---|---|---|---|
| `ELASTIC_ES_URL` | Elasticsearch endpoint the app talks to. | ✅ Basic is free | `http://localhost:9200` |
| `ELASTIC_API_KEY` | Encoded API key (`base64(id:api_key)`) sent as `Authorization: ApiKey …`. Preferred over username/password. | ✅ Security is in Basic since 6.8/7.1 | `UHJvYmVEZXZLZXlJZDpQcm9iZURldkFwaUtleVNlY3JldFZhbHVl` |
| `ELASTIC_USERNAME` | Fallback Basic auth user, used only when `ELASTIC_API_KEY` is empty. | ✅ | `elastic` |
| `ELASTIC_PASSWORD` | Fallback Basic auth password. | ✅ | `probe-dev-es-passw0rd` |
| `ELASTIC_VERIFY_TLS` | Verify the ES/Kibana certificate. Keep `true` outside local dev. | ✅ | `false` (local only) |
| `ELASTIC_CA_CERT_PATH` | PEM path when ES uses a private CA. Blank for plain HTTP dev. | ✅ | *(empty)* |

Cluster privileges the `ELASTIC_API_KEY` needs: `manage_connector`, `manage_api_key`,
`monitor`, plus `read`/`write`/`manage` on the connector's index pattern. All are
Basic-licence features.

## 3. Kibana

| Key | Purpose | Free tier? | Assumed value |
|---|---|---|---|
| `KIBANA_URL` | Kibana endpoint. Action connectors live here, not on Elasticsearch. | ✅ | `http://localhost:5601` |
| `KIBANA_API_KEY` | Auth for the Kibana actions API. Falls back to `ELASTIC_API_KEY`, then to username/password. | ✅ | *(reuses `ELASTIC_API_KEY`)* |

Kibana requires the `kbn-xsrf: true` header on every non-GET request; the app's
Kibana client sets it unconditionally.

> ⚠️ **Licence gate.** Creating the `.jira` action connector needs **Gold or above**
> (`minimumLicenseRequired: 'gold'`). It will fail on a free/Basic stack. This is
> expected and handled: the app probes `GET /api/actions/connector_types`, reports
> the result in the UI capability matrix, and routes ticket creation directly to Jira
> instead. No key can unlock this — it is a licence, not a credential.

## 4. Elasticsearch content connector

| Key | Purpose | Free tier? | Assumed value |
|---|---|---|---|
| `ES_CONNECTOR_ID` | Document id of the connector under `_connector/`. | ✅ | `probe-jira-connector` |
| `ES_CONNECTOR_INDEX` | Index the synced Jira content lands in. Must start with `search-` for Kibana's connector UI to adopt it. | ✅ | `search-jira-probe` |
| `ES_CONNECTOR_NAME` | Display name in Kibana. | ✅ | `Probe Jira content connector` |
| `ES_CONNECTOR_SERVICE_TYPE` | Fixed connector type. | ✅ | `jira` |
| `ES_CONNECTOR_SYNC_INTERVAL` | Cron for the scheduled full sync. | ✅ | `0 0 */3 * * ?` (every 3h) |
| `ES_CONNECTOR_API_KEY` | API key the `elastic-connectors` service uses. Minted by the app via `POST _security/api_key`; write it into `connectors-config/config.yml`. | ✅ | *(generated at provision time)* |

Connector configuration values (`data_source`, `jira_url`, `account_email`,
`api_token`, `projects`, `ssl_enabled`, `retry_count`, `concurrent_downloads`) are
derived from the Jira keys in §1 — they are **not** separate settings, which is the
entire point of the app: one credential entry, both systems configured.

`use_document_level_security` is left `false`: it is a paid-tier feature.

## 5. This application

| Key | Purpose | Free tier? | Assumed value |
|---|---|---|---|
| `PROBE_SECRET_KEY` | Fernet key encrypting stored credentials at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. | n/a | `cHJvYmVfZGV2X2Zlcm5ldF9rZXlfMzJieXRlc19hYmNkZWY9` |
| `PROBE_API_KEY` | Shared secret the React frontend sends as `X-Probe-Key` so the setup API is not world-open. | n/a | `probe-local-dev-key-7f3a9c` |
| `PROBE_STATE_PATH` | Where the encrypted connection state is persisted. | n/a | `./probe-state.json` |
| `PROBE_CORS_ORIGINS` | Allowed browser origins. | n/a | `http://localhost:5173` |
| `PROBE_TICKET_ROUTE` | `auto` \| `elastic_connector` \| `direct_jira`. `auto` picks by licence. | n/a | `auto` |

## 6. Frontend

| Key | Purpose | Assumed value |
|---|---|---|
| `VITE_API_BASE_URL` | Backend origin. | `http://localhost:8000` |
| `VITE_PROBE_API_KEY` | Mirrors `PROBE_API_KEY`. | `probe-local-dev-key-7f3a9c` |

> The frontend is a client-side SPA and holds no Jira or Elastic credential. Those
> are entered in the UI, posted once to the backend, encrypted with `PROBE_SECRET_KEY`
> and never returned — the API only ever echoes masked values such as
> `ATATT3xF…9c1f4e2a`.

---

## Free-tier summary

| Thing | Free? | Note |
|---|---|---|
| Jira Cloud API token | ✅ | Free plan, up to 10 users |
| Jira create issue / JQL search via REST v3 | ✅ | Needs `CREATE_ISSUES` + `BROWSE_PROJECTS` |
| Elasticsearch + Kibana, Basic licence | ✅ | Self-managed |
| `_connector` APIs, self-managed Jira content connector | ✅ | Connector service runs in Docker |
| Elastic-managed (native) connector | ❌ | Paid Cloud tier; app defaults to self-managed |
| Kibana `.jira` action connector | ❌ | **Gold+**; app falls back to direct Jira |
| Connector document-level security | ❌ | Paid; left disabled |

**Nothing in the critical path requires a paid licence.** The only paid feature the
app would *like* is the Kibana Jira action connector, and it degrades cleanly.
